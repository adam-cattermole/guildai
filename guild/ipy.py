# Copyright 2017-2019 TensorHub, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import datetime
import functools
import inspect
import os
import sys
import threading

import six

import pandas as pd

from guild import config
from guild import index2 as indexlib
from guild import op as oplib
from guild import opref as opreflib
from guild import run as runlib
from guild import run_util
from guild import summary
from guild import util
from guild import var

RUN_DETAIL = [
    "id",
    "operation",
    "status",
    "started",
    "stopped",
    "label",
    "run_dir",
]

class RunError(Exception):

    def __init__(self, run, e):
        super(RunError, self).__init__(run, e)
        self.run = run
        self.e = e

class OutputTee(object):

    def __init__(self, fs, lock):
        self._fs = fs
        self._lock = lock

    def write(self, b):
        with self._lock:
            for f in self._fs:
                f.write(b)

class RunOutput(object):

    def __init__(self, run, cb=None):
        self.run = run
        self.cb = cb
        self._f = None
        self._f_lock = None
        self._stdout = None
        self._stderr = None

    def __enter__(self):
        self._f = open(self.run.guild_path("output"), "wb")
        self._f_lock = threading.Lock()
        self._stdout = sys.stdout
        sys.stdout = OutputTee(self._tee_fs(sys.stdout), self._f_lock)
        self._stderr = sys.stderr
        sys.stderr = OutputTee(self._tee_fs(sys.stderr), self._f_lock)

    def _tee_fs(self, iof):
        fs = [iof, self._f]
        if self.cb:
            fs.append(self.cb)
        return fs

    def __exit__(self, *exc):
        with self._f_lock:
            self._f.close()
        sys.stdout = self._stdout
        sys.stderr = self._stderr

@functools.total_ordering
class RunIndex(object):

    def __init__(self, run, fmt):
        self.run = run
        self.fmt = fmt

    def __str__(self):
        return self.run.short_id

    def __eq__(self, x):
        return self._x_id(x) == self.run.id

    def __lt__(self, x):
        return self.run.id < self._x_id(x)

    @staticmethod
    def _x_id(x):
        if isinstance(x, six.string_types):
            return x
        elif isinstance(x, RunIndex):
            return x.run.id
        return None

class RunsSeries(pd.Series):

    @property
    def _constructor(self):
        return RunsSeries

    @property
    def _constructor_expanddim(self):
        return RunsDataFrame

    def delete(self, **kw):
        self.to_frame().delete(**kw)

    def info(self, **kw):
        _print_run_info(self[0], **kw)

    def scalars(self):
        return _runs_scalars([self[0].run])

    def flags(self):
        return _runs_flags([self[0].run])

    def compare(self):
        return _runs_compare([self[0]])

class RunsDataFrame(pd.DataFrame):

    @property
    def _constructor(self):
        return RunsDataFrame

    @property
    def _constructor_sliced(self):
        return RunsSeries

    def delete(self, permanent=False):
        runs = self._runs()
        var.delete_runs(runs, permanent)
        return [run.id for run in runs]

    def _runs(self):
        return [row[1][0].run for row in self.iterrows()]

    def _items(self):
        return [row[1][0] for row in self.iterrows()]

    def info(self, **kw):
        self.loc[0].info(**kw)

    def scalars(self):
        return _runs_scalars(self._runs())

    def flags(self):
        return _runs_flags(self._runs())

    def compare(self):
        return _runs_compare(self._items())

def run(op, *args, **kw):
    opts = _pop_opts(kw)
    run = _init_run()
    _init_run_attrs(run, op, kw, opts)
    summary = _init_output_scalars(run, opts)
    try:
        with RunOutput(run, summary):
            with util.Chdir(run.path):
                result = op(*args, **kw)
    except Exception as e:
        exit_status = 1
        raise RunError(run, e)
    else:
        exit_status = 0
        return run, result
    finally:
        _finalize_run_attrs(run, exit_status)

def _pop_opts(kw):
    opts = {}
    for name in list(kw):
        if name[:1] == "_":
            opts[name[1:]] = kw.pop(name)
    return opts

def _init_run():
    run_id = runlib.mkid()
    run_dir = os.path.join(var.runs_dir(), run_id)
    run = runlib.Run(run_id, run_dir)
    run.init_skel()
    return run

def _init_run_attrs(run, op, kw, opts):
    opref = opreflib.OpRef("func", "", "", "", op.__name__)
    run.write_opref(opref)
    run.write_attr("started", runlib.timestamp())
    run.write_attr("flags", _op_flags(op, kw))
    if "label" in opts:
        run.write_attr("label", opts["label"])

def _op_flags(op, kw):
    return inspect.getcallargs(op, **kw)

def _init_output_scalars(run, opts):
    config = opts.get("output_scalars", oplib.DEFAULT_OUTPUT_SCALARS)
    if not config:
        return None
    abs_guild_path = os.path.abspath(run.guild_path())
    return summary.OutputScalars(config, abs_guild_path)

def _finalize_run_attrs(run, exit_status):
    run.write_attr("exit_status", exit_status)
    run.write_attr("stopped", runlib.timestamp())

def runs():
    runs = var.runs(sort=["-timestamp"])
    data, cols = _format_runs(runs)
    return RunsDataFrame(data=data, columns=cols)

def _format_runs(runs):
    cols = (
        "run",
        "operation",
        "started",
        "status",
        "label",
    )
    data = [_format_run(run, cols) for run in runs]
    return data, cols

def _format_run(run, cols):
    fmt = run_util.format_run(run)
    return [
        _run_attr(run, name, fmt) for name in cols
    ]

def _run_attr(run, name, fmt):
    if name == "run":
        return RunIndex(run, fmt)
    elif name in ("operation",):
        return fmt[name]
    elif name in ("started", "stopped"):
        return _datetime(run.get(name))
    elif name in ("label",):
        return run.get(name, "")
    elif name == "time":
        return util.format_duration(
            run.get("started"),
            run.get("stopped"))
    else:
        return getattr(run, name)

def _datetime(ts):
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(int(ts / 1000000))

def _print_run_info(item, output=False, scalars=False):
    for name in RUN_DETAIL:
        print("%s: %s" % (name, item.fmt.get(name, "")))
    print("flags:", end="")
    print(run_util.format_attr(item.run.get("flags", "")))
    if scalars:
        print("scalars:")
        for s in indexlib.iter_run_scalars(item.run):
            print(
                "  %s: %f (step %i)"
                % (s["tag"], s["last_val"], s["last_step"]))
    if output:
        print("output:")
        for line in run_util.iter_output(item.run):
            print("  %s" % line, end="")

def _runs_scalars(runs):
    data = []
    for run in runs:
        for s in indexlib.iter_run_scalars(run):
            data.append(s)
    return pd.DataFrame(data)

def _runs_flags(runs):
    data = [_run_flags_data(run) for run in runs]
    return pd.DataFrame(data)

def _run_flags_data(run):
    data = run.get("flags") or {}
    data[_run_flags_key(data)] = run.id
    return data

def _run_flags_key(flags):
    run_key = "run"
    while run_key in flags:
        run_key = "_" + run_key
    return run_key

def _runs_compare(items):
    core_cols = ["run", "operation", "time", "status", "label"]
    flag_cols = set()
    scalar_cols = set()
    data = []
    for item in items:
        row_data = {}
        data.append(row_data)
        # Order matters here - we want flag vals to take precedence
        # over scalar vals with the same name.
        _apply_scalar_data(item.run, scalar_cols, row_data)
        _apply_flag_data(item.run, flag_cols, row_data)
        _apply_run_core_data(item, core_cols, row_data)
    cols = (
        core_cols +
        sorted(flag_cols) +
        _sort_scalar_cols(scalar_cols, flag_cols))
    return pd.DataFrame(data, columns=cols)

def _apply_scalar_data(run, cols, data):
    for name, val in _run_scalar_data(run).items():
        cols.add(name)
        data[name] = val

def _run_scalar_data(run):
    data = {}
    step = None
    last_step = None
    for s in indexlib.iter_run_scalars(run):
        key = s["tag"]
        data[key] = s["last_val"]
        last_step = s["last_step"]
        if key == "loss":
            step = last_step
    if data:
        if step is None:
            step = last_step
        data["step"] = step
    return data

def _apply_flag_data(run, cols, data):
    for name, val in _run_flags_data(run).items():
        if name == "run":
            continue
        cols.add(name)
        data[name] = val

def _apply_run_core_data(item, cols, data):
    for name in cols:
        data[name] = _run_attr(item.run, name, item.fmt)

def _sort_scalar_cols(scalar_cols, flag_cols):
    # - List step first if it exists
    # - Don't include flag cols in result
    cols = []
    if "step" in scalar_cols:
        cols.append("step")
    for col in sorted(scalar_cols):
        if col == "step" or col in flag_cols:
            continue
        cols.append(col)
    return cols

def set_guild_home(path):
    config.set_guild_home(path)