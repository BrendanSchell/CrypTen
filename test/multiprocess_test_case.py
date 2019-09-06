#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import logging
import multiprocessing
import os
import sys
import tempfile
import unittest
from functools import wraps

import numpy as np
import torch
import torch.distributed as dist

from .benchmark_helper import BenchmarkHelper


def get_random_test_tensor(max_value=6,
                           size=(1, 5),
                           is_float=False,
                           ex_zero=False):
    """Generates random tensor for testing

    Args:
        max_value (int): defines range for int tensor
        size (tuple): size of tensor
        is_float (bool): determines float or int tensor
        ex_zero (bool): excludes zero tensor

    Returns: torch.tensor
    """
    if is_float:
        tensor = (2 * torch.rand(torch.Size(size)) - 1) * max_value
    else:
        tensor = torch.randint(
            -max_value, max_value, torch.Size(size), dtype=torch.int64
        )
    if ex_zero:
        # replace 0 with 1
        tensor[tensor == 0] = 1

    if dist.is_initialized():
        # Broadcast this tensor to the world so that the generated random tensor
        # is in sync in all distributed processes. See T45688819 for more
        # information.
        dist.broadcast(tensor, 0)

    return tensor


def get_random_linear(in_channels, out_channels):
    linear = torch.nn.Linear(in_channels, out_channels)
    if dist.is_initialized():
        # Broadcast this tensor to the world so that the generated random tensor
        # is in sync in all distributed processes. See T45688819 for more
        # information.
        dist.broadcast(linear.weight, 0)
        dist.broadcast(linear.bias, 0)

    return linear


class MultiProcessTestCase(unittest.TestCase):
    MAIN_PROCESS_RANK = -1

    @property
    def world_size(self):
        return 2

    @staticmethod
    def join_or_run(fn):
        @wraps(fn)
        def wrapper(self):
            if self.rank == self.MAIN_PROCESS_RANK:
                self._join_processes(fn)
            else:
                fn(self)

        return wrapper

    # The main process spawns N subprocesses that run the test.
    # This function patches overwrites every test function to either
    # assume the role of the main process and join its subprocesses,
    # or run the underlying test function.
    @classmethod
    def setUpClass(cls):
        try:
            multiprocessing.set_start_method("spawn")
        except RuntimeError:
            logging.warning("Failed to set start method to spawn")
            pass

        for attr in dir(cls):
            if attr.startswith("test"):
                fn = getattr(cls, attr)
                setattr(cls, attr, cls.join_or_run(fn))

        cls.benchmark_helper = None


    def __init__(self, methodName):
        super().__init__(methodName)

        self.rank = self.MAIN_PROCESS_RANK

    def setUp(self):
        super(MultiProcessTestCase, self).setUp()

        self.benchmark_iters = 100 if self.benchmarks_enabled else 1
        self.default_tolerance = 0.5

        cls = self.__class__
        cls.benchmark_helper = BenchmarkHelper(self.benchmarks_enabled,
                                               self.benchmark_iters)
        if hasattr(self, 'benchmark_queue'):
            cls.benchmark_helper.queue = self.benchmark_queue

        # This gets called in the children process as well to give subclasses a
        # chance to initialize themselves in the new process
        if self.rank == self.MAIN_PROCESS_RANK:
            self.file = tempfile.NamedTemporaryFile(delete=True).name


            self.processes = [
                self._spawn_process(rank) for rank in range(int(self.world_size))
            ]

    def tearDown(self):
        super(MultiProcessTestCase, self).tearDown()
        self.__class__.benchmark_helper.drain_benchmark_queue()
        for p in self.processes:
            p.terminate()

    @classmethod
    def tearDownClass(cls):
        if cls.benchmark_helper:
            cls.benchmark_helper.print_benchmark_summary(cls.__name__)

    def _current_test_name(self):
        # self.id() == e.g. '__main__.TestDistributed.TestAdditive.test_get_rank'
        return self.id().split(".")[-1]

    def _spawn_process(self, rank):
        name = "process " + str(rank)
        test_name = self._current_test_name()
        process = multiprocessing.Process(
            target=self.__class__._run,
            name=name,
            args=(test_name, rank, self.__class__.benchmark_helper.queue, self.file),
        )
        process.start()
        return process

    @classmethod
    def _run(cls, test_name, rank, queue, file):
        self = cls(test_name)

        self.file = file
        self.rank = int(rank)
        self.benchmark_queue = queue

        # set environment variables:
        communicator_args = {
            "WORLD_SIZE": self.world_size,
            "RANK": self.rank,
            "RENDEZVOUS": "file://%s" % self.file,
            "BACKEND": "gloo",
        }
        for key, val in communicator_args.items():
            os.environ[key] = str(val)

        self.setUp()

        # We're retrieving a corresponding test and executing it.
        getattr(self, test_name)()
        sys.exit(0)

    def _join_processes(self, fn):
        for p in self.processes:
            p.join()
            self._check_return_codes(p)

    def _check_return_codes(self, process):
        self.assertEqual(process.exitcode, 0)

    def benchmark(self, niters=None, data=None, **kwargs):
        return self.benchmark_helper.benchmark(self, niters, data, **kwargs)
