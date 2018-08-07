"""
Benchmarking convolution performance for Android RPC.
===========================================================

To use it in remote, start a rpc proxy with "python -m tvm.exec.rpc_tracker --port 9090"
"""


from __future__ import absolute_import, print_function

import time
import os
import sys
import tvm
from tvm import rpc, autotvm
from tvm.contrib import util, ndk, cc
import topi
from topi.util import get_const_tuple
import numpy as np
import contextlib

from data_type import get_data_type
from correctness import test_correctness
from workloads import get_workloads
from utils import get_input_and_filter_shape, config_arch, get_num_ops

import logging
logging.basicConfig(level=logging.DEBUG)

# Set it to be address of tvm proxy.
tracker_host = os.environ["TVM_TRACKER_HOST"]
tracker_port = int(os.environ["TVM_TRACKER_PORT"])
key = "android"
log_file = "convolutions.log"


@contextlib.contextmanager
def dummy_context_mgr():
    yield None


def build_and_run(phase, idx,
                  input_holder, filter_holder, kernel, space,
                  input_channel, output_channel,
                  stride, pad, layout,
                  tvm_input, tvm_filter, tvm_output,
                  ctx, ts, conv,
                  target, target_host,
                  warmup, run, ops,
                  remote):
    with tvm.build_config():
        f_name = str(idx) + "_" + phase + "_" + str(kernel) + "_" + \
            str(space) + "_" + str(input_channel) + "_" + str(output_channel)
        # Uncomment this line if you want to look up IR codes
        #print(tvm.lower(ts, [input_holder, filter_holder, conv], name=f_name+".S", simple_mode=True))

        # I am not sure if the configuration is runnable or not, so wrap it by
        # a try and except
        try:
            f = tvm.build(ts, [input_holder, filter_holder, conv],
                          target=target, target_host=target_host, name=f_name)
        except BaseException as e:
            print(e)
            print(
                "{0}--target: {1}, dtype: {2}, layout: {3}, stride: {4}, pad: {5}, input_shape: {6}, filter_shape: {7} -> failed".format(
                    phase, target, str(
                        tvm_input.dtype), layout, str(stride), str(pad), str(
                        input_holder.shape), str(
                        filter_holder.shape)))
        else:
            # Upload code
            if remote is not None:
                so_name = f_name + ".so"
                temp = util.tempdir()
                path_so = temp.relpath(so_name)
                f.export_library(path_so, ndk.create_shared)
                remote.upload(path_so)
                f = remote.load_module(so_name)

            # Warmup runs
            for _ in range(warmup):
                f(tvm_input, tvm_filter, tvm_output)

            # Evaluate runs
            timer = f.time_evaluator(f.entry_name, ctx, number=run)
            cost = timer(tvm_input, tvm_filter, tvm_output).mean
            print(
                "{0}--target: {1}, dtype: {2}, layout: {3}, stride: {4}, pad: {5}, input_shape: {6}, filter_shape: {7}, ops: {8} -> {9}".format(
                    phase, target, str(
                        tvm_input.dtype), layout, str(stride), str(pad), str(
                        input_holder.shape), str(
                        filter_holder.shape), str(ops), cost))

            # Test correctness by comparing to caffe2 results
            test_correctness(
                input_channel,
                output_channel,
                kernel,
                stride,
                pad,
                tvm_input.asnumpy(),
                tvm_filter.asnumpy(),
                tvm_output.asnumpy(),
                dtype=tvm_input.dtype,
                order=layout,
                depthwise=True if phase == "depthwise" else False)


def get_conv_ts(
        input_holder,
        filter_holder,
        stride,
        pad,
        layout,
        dtype,
        depthwise):
    if dtype.tvm_type() == "int8":
        output_type = "int32"
    else:
        output_type = dtype.tvm_type()
    if not depthwise:
        # s1
        conv = topi.nn.conv2d(
            input_holder,
            filter_holder,
            [stride, stride],
            [pad, pad],
            layout,
            out_dtype=output_type)
        if layout == "NCHW":
            ts = topi.generic.schedule_conv2d_nchw([conv])
        elif layout == "NHWC":
            ts = topi.generic.schedule_conv2d_nhwc([conv])
        elif layout == "HWCN":
            ts = topi.generic.schedule_conv2d_hwcn([conv])
        # s2
        #conv = topi.nn.conv2d(input_holder, filter_holder, 1, 0, layout)
        #ts = tvm.create_schedule(conv.op)
    else:
        if layout == "NCHW":
            conv = topi.nn.depthwise_conv2d_nchw(
                input_holder, filter_holder, [
                    stride, stride], pad, out_dtype=output_type)
            ts = topi.generic.schedule_depthwise_conv2d_nchw([conv])
        elif layout == "NHWC":
            conv = topi.nn.depthwise_conv2d_nhwc(
                input_holder, filter_holder, [
                    stride, stride], pad, out_dtype=output_type)
            ts = topi.generic.schedule_depthwise_conv2d_nhwc([conv])
        elif layout == "HWCN":
            conv = topi.nn.depthwise_conv2d_hwcn(
                input_holder, filter_holder, [
                    stride, stride], pad, out_dtype=output_type)
            ts = topi.generic.schedule_depthwise_conv2d_hwcn([conv])
    return conv, ts


def bench_tvm(
        arch,
        tgt,
        dtype,
        layout,
        workloads,
        remote,
        schedule):
    target, target_host, ctx = config_arch(tgt, arch, schedule, remote)
    if target is None:
        return

    for idx, workload in enumerate(workloads):
        space = workload.space()
        input_channel = workload.input_channel()
        output_channel = workload.output_channel()
        kernel = workload.kernel()
        pad = workload.pad()
        stride = workload.stride()
        warmup = workload.warmup()
        run = workload.run()
        phase = "depthwise" if workload.depthwise() else "standard"

        (input_shape, filter_shape) = get_input_and_filter_shape(
            layout, space, input_channel, output_channel, kernel, workload.depthwise())
        input_holder = tvm.placeholder(input_shape, dtype=dtype.tvm_type())
        filter_holder = tvm.placeholder(filter_shape, dtype=dtype.tvm_type())
        stride_holder = tvm.var("s")
        padding_holder = tvm.var("p")

        input_data = np.random.random(input_shape)
        filter_data = np.random.random(filter_shape)

        log_name = "../configs/" + key + "." + log_file
        # create schedule
        with autotvm.apply_history_best(log_name) if schedule == "manual" else dummy_context_mgr():
            with tvm.target.create(target):
                try:
                    conv, ts = get_conv_ts(
                        input_holder, filter_holder, stride, pad, layout, dtype, workload.depthwise())
                except BaseException as e:
                    print(e)
                    print(
                        "standard--target: {0}, dtype: {1}, layout: {2}, input_shape: {3}, filter_shape: {4} -> schedule skip".format(
                            target, str(
                                input_holder.dtype), layout, str(
                                input_holder.shape), str(
                                filter_holder.shape)))
                    continue
                else:
                    try:
                        tvm_input = tvm.nd.array(
                            input_data.astype(dtype.np_type()), ctx)
                        tvm_filter = tvm.nd.array(
                            filter_data.astype(dtype.np_type()), ctx)
                        tvm_output = tvm.nd.array(
                            np.zeros(
                                get_const_tuple(
                                    conv.shape),
                                dtype=conv.dtype),
                            ctx)
                        if layout == "NCHW":
                            output_space = get_const_tuple(conv.shape)[2]
                        elif layout == "NHWC":
                            output_space = get_const_tuple(conv.shape)[1]
                        elif layout == "HWCN":
                            output_space = get_const_tuple(conv.shape)[0]
                        ops = get_num_ops(
                            output_space,
                            input_channel,
                            output_channel,
                            kernel,
                            workload.depthwise())

                        build_and_run(phase, idx,
                                      input_holder, filter_holder,
                                      kernel, space,
                                      input_channel, output_channel,
                                      stride, pad, layout,
                                      tvm_input, tvm_filter, tvm_output,
                                      ctx, ts, conv,
                                      target, target_host,
                                      warmup, run, ops,
                                      remote)
                    except BaseException as e:
                        print(e)
                        print(
                            "{0}--target: {1}, dtype: {2}, layout: {3}, input_shape: {4}, filter_shape: {5} -> run skip".format(
                                phase, target, str(
                                    input_holder.dtype), layout, str(
                                    input_holder.shape), str(
                                    filter_holder.shape)))
                    else:
                        continue


if __name__ == "__main__":
    arch = sys.argv[1]
    if sys.argv[2] == "remote":
        # Connect to the proxy
        key = sys.argv[3]
        tracker = rpc.connect_tracker(tracker_host, tracker_port)
        remote = tracker.request(key)
    else:
        remote = None

    if len(sys.argv) > 4:
        target = sys.argv[4]
        dtype = get_data_type(sys.argv[5])
        layout = sys.argv[6]
        workloads = get_workloads(sys.argv[7])
        schedule = sys.argv[8]

        bench_tvm(
            arch,
            target,
            dtype,
            layout,
            workloads,
            remote,
            schedule)
    else:
        for target in ["cpu"]:
            for dtype in ["int8", "float"]:
                for layout in ["NCHW", "NHWC", "HWCN"]:
                    for workloads in [
                        "caffe2_depthwise",
                        "caffe2_standard",
                            "mobilenet"]:
                        for schedule in ["auto", "manual"]:
                            bench_tvm(
                                arch,
                                target,
                                get_data_type(dtype),
                                layout,
                                get_workloads(workloads),
                                remote,
                                schedule)
