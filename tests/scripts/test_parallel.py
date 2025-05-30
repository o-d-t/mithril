# Copyright 2022 Synnada, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math
import queue
import threading
import time
from copy import deepcopy
from typing import Any

import jax
import numpy as np
import pytest
import torch
import torch.distributed
from torch.distributed._tensor import (
    Replicate,
    Shard,
)

import mithril
from mithril import compile
from mithril.backends.with_autograd.torch_backend.parallel import TorchParallel
from mithril.backends.with_autograd.torch_backend.utils import SharedCyclicQueue
from mithril.framework.common import Tensor
from mithril.models import (
    TBD,
    Add,
    Eye,
    IOKey,
    Linear,
    Model,
    Multiply,
    Relu,
    Sigmoid,
    Tanh,
    ToTensor,
)

current_device_mesh: tuple[int, ...] | None = None


def create_parallel_backend(device_mesh: tuple[int, ...]):
    global current_device_mesh
    if current_device_mesh is not None and math.prod(device_mesh) != math.prod(
        current_device_mesh
    ):
        if TorchParallel._instance is not None:
            TorchParallel._instance.clean_up()
        current_device_mesh = None

    backend = mithril.TorchBackend(device_mesh=device_mesh)
    current_device_mesh = device_mesh
    return backend


def test_torch_shared_cyclic_queue_1():
    # Write basic instruction with empty args and kwargs
    writer = SharedCyclicQueue(2)
    reader = deepcopy(writer)
    writer.write(opcode1=1, opcode2=4, args=())

    offset = 2
    buffer = writer._shm.buf
    instruction = "".join(
        format(byte, "08b") for byte in buffer[offset + 8 : offset + 12][::-1]
    )

    assert reader.read(rank=1) == (1, 4, [], {})
    assert instruction[0] == "0"  # Pickle should not be necessary
    assert instruction[1] == "0"  # There is not any kwargs


def test_torch_shared_cyclic_queue_2():
    # Write basic instruction with args
    writer = SharedCyclicQueue(2)
    reader = deepcopy(writer)
    writer.write(opcode1=1, opcode2=4, args=(5.0, 3))

    offset = 2
    buffer = writer._shm.buf
    instruction = "".join(
        format(byte, "08b") for byte in buffer[offset + 8 : offset + 12][::-1]
    )

    assert reader.read(rank=1) == (1, 4, [5.0, 3], {})
    assert instruction[0] == "0"
    assert instruction[1] == "0"


def test_torch_shared_cyclic_queue_3():
    # Write basic instruction with args and kwargs
    writer = SharedCyclicQueue(2)
    reader = deepcopy(writer)
    writer.write(
        opcode1=1,
        opcode2=4,
        args=(5, 3.0),
        kwargs={"input": "something", "left": (1, 2, (3, 4))},
    )

    offset = 2
    buffer = writer._shm.buf
    instruction = "".join(
        format(byte, "08b") for byte in buffer[offset + 8 : offset + 12][::-1]
    )

    assert reader.read(rank=1) == (
        1,
        4,
        [5, 3.0],
        {"input": "something", "left": (1, 2, (3, 4))},
    )
    assert instruction[0] == "0"
    assert instruction[1] == "1"


def test_torch_shared_cyclic_queue_4():
    # Write basic instruction with args and kwargs, args need pickle
    writer = SharedCyclicQueue(2)
    reader = deepcopy(writer)
    writer.write(
        opcode1=1,
        opcode2=4,
        args=(5, 5, (4, 2)),
        kwargs={"input": "something", "left": (1, 2, (3, 4))},
    )

    offset = 2
    buffer = writer._shm.buf
    instruction = "".join(
        format(byte, "08b") for byte in buffer[offset + 8 : offset + 12][::-1]
    )

    assert reader.read(rank=1) == (
        1,
        4,
        (5, 5, (4, 2)),
        {"input": "something", "left": (1, 2, (3, 4))},
    )
    assert instruction[0] == "1"
    assert instruction[1] == "1"


def test_torch_shared_cyclic_queue_5():
    # Reader cannot read before writer
    writer_queue = SharedCyclicQueue(2)
    reader_queue = deepcopy(writer_queue)

    message_queue: queue.Queue = queue.Queue()

    def reader(shared_queue: SharedCyclicQueue, message_queue: queue.Queue, rank: int):
        message = shared_queue.read(rank)
        message_queue.put("reader")
        message_queue.put(message)

    def writer(
        shared_queue: SharedCyclicQueue, message_queue: queue.Queue, message: Any
    ):
        time.sleep(0.1)
        shared_queue.write(*message)
        message_queue.put("writer")

    thread_reader = threading.Thread(
        target=reader, args=(reader_queue, message_queue, 1)
    )
    thread_writer = threading.Thread(
        target=writer,
        args=(writer_queue, message_queue, (1, 4, [4, 4], {"input": 123})),
    )

    thread_writer.start()
    thread_reader.start()

    thread1 = message_queue.get()
    thread2 = message_queue.get()
    message = message_queue.get()

    offset = 2
    buffer = writer_queue._shm.buf
    instruction = "".join(
        format(byte, "08b") for byte in buffer[offset + 8 : offset + 12][::-1]
    )

    assert thread1 == "writer"
    assert thread2 == "reader"
    assert instruction[0] == "0"
    assert instruction[1] == "1"
    assert message == (1, 4, [4, 4], {"input": 123})


def test_torch_shared_cyclic_queue_6():
    # Multiple reader reads same message
    writer_queue = SharedCyclicQueue(3)
    reader_queue1 = deepcopy(writer_queue)
    reader_queue2 = deepcopy(writer_queue)
    message_queue: queue.Queue = queue.Queue()

    def reader(shared_queue: SharedCyclicQueue, message_queue: queue.Queue, rank: int):
        message = shared_queue.read(rank)
        message_queue.put("reader")
        message_queue.put(message)

    def writer(
        shared_queue: SharedCyclicQueue, message_queue: queue.Queue, message: Any
    ):
        time.sleep(0.1)
        shared_queue.write(*message)
        message_queue.put("writer")

    thread_reader1 = threading.Thread(
        target=reader, args=(reader_queue1, message_queue, 1)
    )
    thread_reader2 = threading.Thread(
        target=reader, args=(reader_queue2, message_queue, 2)
    )
    thread_writer = threading.Thread(
        target=writer,
        args=(writer_queue, message_queue, (1, 4, [4, 4], {"input": 123})),
    )

    thread_writer.start()
    thread_reader1.start()
    thread_reader2.start()

    messages = [message_queue.get() for _ in range(5)]

    offset = 3
    buffer = writer_queue._shm.buf
    instruction = "".join(
        format(byte, "08b") for byte in buffer[offset + 8 : offset + 12][::-1]
    )

    assert instruction[0] == "0"
    assert instruction[1] == "1"
    assert messages[0] == "writer"

    for message in messages[1:]:
        if isinstance(message, str):
            assert message == "reader"
        else:
            assert message == (1, 4, [4, 4], {"input": 123})


def test_torch_shared_cyclic_queue_7():
    # Write must wait redaer
    shared_queue = SharedCyclicQueue(3)
    writer_queue2 = deepcopy(shared_queue)
    reader_queue1 = deepcopy(shared_queue)
    reader_queue2 = deepcopy(shared_queue)
    message_queue: queue.Queue = queue.Queue()

    def reader(shared_queue: SharedCyclicQueue, message_queue: queue.Queue, rank: int):
        for _ in range(2):
            shared_queue.read(rank)
            message_queue.put("reader")

    def writer(
        shared_queue: SharedCyclicQueue, message_queue: queue.Queue, message: Any
    ):
        time.sleep(0.1)
        shared_queue.write(*message)
        message_queue.put("writer")

    thread_reader1 = threading.Thread(
        target=reader, args=(reader_queue1, message_queue, 1)
    )
    thread_reader2 = threading.Thread(
        target=reader, args=(reader_queue2, message_queue, 2)
    )
    thread_writer = threading.Thread(
        target=writer,
        args=(writer_queue2, message_queue, (1, 4, [4, 4], {"input": 123})),
    )

    thread_reader1.start()
    thread_reader2.start()

    # Write one first message so reader moves one step forward
    shared_queue.write(5, 5, (5, 5))
    # Get messages
    messages = message_queue.get()
    message_queue.get()

    # It won't be able to write
    thread_writer.start()

    with pytest.raises(queue.Empty):
        message_queue.get(timeout=1)

    # Write another messages to allow writer can write
    shared_queue.write(5, 5, (5, 5))
    messages = [message_queue.get() for _ in range(3)]

    assert {"reader", "writer"} == set(messages)


def test_torch_parallel_1():
    model = Model()
    model += Linear(256)(input="input", weight="w", bias="b")
    backend = mithril.TorchBackend()
    backend_parallel = create_parallel_backend(device_mesh=(4,))

    pm = compile(
        model, backend, jit=True, shapes={"input": [8, 128]}, data_keys={"input"}
    )
    pm_parallel = compile(
        model,
        backend_parallel,
        jit=True,
        shapes={"input": [8, 128]},
        data_keys={"input"},
    )

    tensor1 = backend.ones(8, 128)

    sharded_tensor = backend_parallel.array(tensor1, device_mesh=(4,))
    replicated_tensor = backend_parallel.array(tensor1)

    assert sharded_tensor._local_tensor.shape == (2, 128)  # type: ignore
    assert replicated_tensor._local_tensor.shape == (8, 128)  # type: ignore

    # Apply op to sharded tensor
    sharded_tensor += 1
    np.testing.assert_allclose(
        sharded_tensor._local_tensor.cpu(),  # type: ignore
        (torch.ones(2, 128) + 1),
    )

    # Get full_tensor from sharded_tensor
    full_tensor = sharded_tensor.full_tensor().cpu()  # type: ignore
    np.testing.assert_allclose(full_tensor, (torch.ones(8, 128) + 1))

    result_tensor = replicated_tensor + sharded_tensor
    assert result_tensor._local_tensor.shape == (2, 128)  # type: ignore
    np.testing.assert_allclose(
        result_tensor.full_tensor().cpu(),  # type: ignore
        (torch.ones(8, 128) + 2),
    )

    params = pm.randomize_params()

    params = {key: backend.ones(value.shape) for key, value in params.items()}
    input = {"input": backend.array(tensor1)}

    params_parallel = {
        key: backend_parallel.ones(value.shape) for key, value in params.items()
    }
    input_parallel = {"input": backend_parallel.array(tensor1, device_mesh=(4,))}

    result_parallel = pm_parallel.evaluate(params_parallel, input_parallel)
    out = result_parallel["output"]
    assert isinstance(out, torch.distributed._tensor.DTensor)
    assert out._local_tensor.shape == (2, 256)

    output_full_tensor = out.full_tensor().cpu()
    np.testing.assert_allclose(output_full_tensor, (torch.ones(8, 256) * 129))

    output_grad = backend.randn(8, 256)
    output_grad_parallel = backend_parallel.array(output_grad)
    _, grads = pm.evaluate(params, input, output_gradients={"output": output_grad})
    _, grads_parallel = pm_parallel.evaluate(
        params_parallel,
        input_parallel,
        output_gradients={"output": output_grad_parallel},
    )

    for key, grad in grads.items():
        parallel_grad = grads_parallel[key].full_tensor()
        np.testing.assert_allclose(grad.cpu(), parallel_grad.cpu(), 1e-6, 1e-6)


def test_torch_parallel_2():
    # This test checks parallel execution with a model that includes array creation
    # primitive eye.
    model = Model()
    model |= (linear := Linear(256))(input="input", weight="w", bias="b")
    model |= (e := Eye(N=TBD))(N=linear.output.shape[0])
    model |= Add()(left=linear.output, right=e.output, output="output")
    backend = create_parallel_backend(device_mesh=(4, 1))
    backend.ones([256])
    pm = compile(model, backend, jit=False, data_keys={"input"})

    params = {"w": backend.ones([256, 128]), "b": backend.ones([256])}

    # Replicate params
    input = {"input": backend.ones(256, 128, device_mesh=(4, 1))}
    result = pm.evaluate(params, input)
    out = result["output"]
    assert isinstance(out, torch.distributed._tensor.DTensor)

    output_full_tensor = out.full_tensor()
    np.testing.assert_allclose(
        output_full_tensor, (torch.ones(256, 256) * 129 + torch.eye(256))
    )


@pytest.mark.skip(reason="Fails because of the torch dist bug.")
def test_torch_parallel_3():
    # This test checks parallel execution with a model that includes array creation
    # primitive to_tensor.
    model = Model()
    model += (linear := Linear(256))(input="input", weight="w", bias="b")
    model += (e := ToTensor())(input=IOKey(name="to_tensor", value=TBD))
    model += Add()(left=linear.output, right=e.output, output="output")
    backend = mithril.TorchBackend()
    backend_parallel = create_parallel_backend(device_mesh=(4, 1))

    pm = compile(model, backend, jit=False, data_keys={"input", "to_tensor"})
    pm_parallel = compile(
        model, backend_parallel, jit=False, data_keys={"input", "to_tensor"}
    )

    params = {"w": backend.ones([256, 128]), "b": backend.ones([256])}
    params_parallel = {
        "w": backend_parallel.ones([128, 256]),
        "b": backend_parallel.ones([256]),
    }

    input: dict[str, torch.Tensor | list[int]] = {
        "input": backend.ones(256, 128),
        "to_tensor": [1, 2, 3, 4] * 64,
    }
    input_parallel = {
        "input": backend_parallel.ones(256, 128, device_mesh=(4, 1)),
        "to_tensor": [1, 2, 3, 4] * 64,
    }

    result_parallel = pm_parallel.evaluate(params_parallel, input_parallel)
    out = result_parallel["output"]
    assert isinstance(out, torch.distributed._tensor.DTensor)
    output_full_tensor = out.full_tensor()
    np.testing.assert_allclose(
        output_full_tensor,
        (torch.ones(256, 256) * 129 + (torch.arange(4).repeat(64) + 1)),
    )

    output_grad = backend.randn(256, 256)
    output_grad_parallel = backend_parallel.array(output_grad)
    _, grads = pm.evaluate(params, input, output_gradients={"output": output_grad})
    _, grads_parallel = pm_parallel.evaluate(
        params_parallel,
        input_parallel,
        output_gradients={"output": output_grad_parallel},
    )

    for key, grad in grads.items():
        parallel_grad = grads_parallel[key].full_tensor()
        np.testing.assert_allclose(grad, parallel_grad, 1e-6, 1e-6)


@pytest.mark.skip("Will be fixed in torch 2.6.0")
def test_torch_parallel_4():
    # This test checks parallel execution with a model that includes immediate values
    # in Add primitive.
    model = Model()
    model += (linear := Linear(256))(input="input", weight="w", bias="b", output="out1")
    model += Add()(left=linear.output, right=[3] * 256, output="output")  # type: ignore

    backend = mithril.TorchBackend()
    backend_parallel = create_parallel_backend(device_mesh=(4, 1))

    pm = compile(model, backend, jit=False, data_keys={"input"})
    pm_parallel = compile(model, backend_parallel, jit=False, data_keys={"input"})

    params = {"w": backend.ones([256, 128]), "b": backend.ones([256])}
    input = {"input": backend.ones(256, 128)}
    params_parallel = {
        "w": backend_parallel.ones([256, 128]),
        "b": backend_parallel.ones([256]),
    }
    input_parallel = {"input": backend_parallel.ones(256, 128, device_mesh=(4, 1))}

    result_parallel = pm_parallel.evaluate(params_parallel, input_parallel)
    out = result_parallel["output"]
    assert isinstance(out, torch.distributed._tensor.DTensor)
    output_full_tensor = out.full_tensor()
    np.testing.assert_allclose(output_full_tensor, torch.ones(256, 256) * 129 + 3)

    output_grad = backend.rand(256, 256)
    output_grad_parallel = backend_parallel.array(output_grad)
    _, grads = pm.evaluate(params, input, output_gradients={"output": output_grad})
    _, grads_parallel = pm_parallel.evaluate(
        params_parallel,
        input_parallel,
        output_gradients={"output": output_grad_parallel},
    )
    for key, grad in grads.items():
        parallel_grad = grads_parallel[key].full_tensor()
        np.testing.assert_allclose(grad, parallel_grad, 1e-6, 1e-6)


def test_torch_parallel_5():
    # This test checks parallel execution with a model that includes cache of
    # primitive eye.
    model = Model()
    model |= (linear := Linear(256))(input="input", weight="w", bias="b")
    model |= (e := Eye(N=TBD))(N=linear.output.shape[0])
    model |= Add()(left=linear.output, right=e.output, output="output")

    backend = mithril.TorchBackend()
    backend_parallel = create_parallel_backend(device_mesh=(2, 2))
    pm = compile(
        model,
        backend,
        jit=False,
        shapes={"input": [256, 128]},
        data_keys={"input"},
    )
    pm_parallel = compile(
        model,
        backend_parallel,
        jit=False,
        shapes={"input": [256, 128]},
        data_keys={"input"},
    )

    input = {"input": backend.ones(256, 128)}
    params = {"w": backend.ones([256, 128]), "b": backend.ones([256])}
    input_parallel = {"input": backend_parallel.ones(256, 128, device_mesh=(2, 2))}
    params_parallel = {
        "w": backend_parallel.ones([256, 128]),
        "b": backend_parallel.ones([256]),
    }

    result = pm.evaluate(params, input)
    result_parallel = pm_parallel.evaluate(params_parallel, input_parallel)

    output_grads = backend.rand(256, 256)
    outout_grads_parallel = backend_parallel.array(output_grads)
    out_parallel = result_parallel["output"]
    out = result["output"]
    assert isinstance(out_parallel, torch.distributed._tensor.DTensor)
    assert isinstance(out, torch.Tensor)

    output_full_tensor = out_parallel.full_tensor()
    np.testing.assert_allclose(output_full_tensor, out)

    _, param_grads = pm.evaluate(
        params, input, output_gradients={"output": output_grads}
    )
    _, param_grads_parallel = pm_parallel.evaluate(
        params_parallel,
        input_parallel,
        output_gradients={"output": outout_grads_parallel},
    )
    for key, grad in param_grads.items():
        parallel_grad = param_grads_parallel[key].full_tensor()
        np.testing.assert_allclose(grad, parallel_grad, 1e-6, 1e-6)


def test_row_wise_sharding():
    backend = mithril.TorchBackend()
    backend_parallel = create_parallel_backend(device_mesh=(1, 4))

    input = backend.rand((32, 32))
    weight = backend.rand((32, 32))

    input_sharded = backend_parallel.array(input, device_mesh=((0, 1), (1, 4)))
    row_wise_weight = backend_parallel.array(weight, device_mesh=((0, 1), (0, 4)))
    assert row_wise_weight.placements == (Replicate(), Shard(0))
    assert row_wise_weight._local_tensor.shape == (8, 32)
    assert input_sharded.placements == (Replicate(), Shard(1))
    assert input_sharded._local_tensor.shape == (32, 8)

    result = input @ weight.T
    result_sharded = input_sharded @ row_wise_weight.T
    assert result_sharded.placements == (Replicate(), Shard(1))
    assert result_sharded._local_tensor.shape == (32, 8)

    assert torch.allclose(result, result_sharded.full_tensor())


def test_col_wise_sharding():
    backend = mithril.TorchBackend()
    backend_parallel = create_parallel_backend(device_mesh=(1, 4))

    input = backend.rand((32, 32))
    weight = backend.rand((32, 32))

    input_sharded = backend_parallel.array(input, device_mesh=((0, 1), (1, 4)))
    col_wise_weight = backend_parallel.array(weight, device_mesh=((0, 1), (1, 4)))
    assert col_wise_weight.placements == (Replicate(), Shard(1))
    assert col_wise_weight._local_tensor.shape == (32, 8)
    assert input_sharded.placements == (Replicate(), Shard(1))
    assert input_sharded._local_tensor.shape == (32, 8)

    result = input @ weight.T
    result_sharded = input_sharded @ col_wise_weight.T
    assert result_sharded.placements == (Replicate(), Shard(1))
    assert result_sharded._local_tensor.shape == (32, 8)

    assert torch.allclose(result, result_sharded.full_tensor())


def test_col_and_row_wise_sharding_reverse():
    backend = mithril.TorchBackend()
    backend_parallel = create_parallel_backend(device_mesh=(2, 2))

    input = backend.rand((32, 32))
    weight = backend.rand((32, 32))

    input_sharded = backend_parallel.array(input, device_mesh=((0, 2), (1, 2)))
    weight_row_col_sharded = backend_parallel.array(
        weight, device_mesh=((1, 2), (0, 2))
    )
    assert weight_row_col_sharded.placements == (Shard(1), Shard(0))
    assert weight_row_col_sharded._local_tensor.shape == (16, 16)

    result = input @ weight
    result_sharded = input_sharded @ weight_row_col_sharded
    assert torch.allclose(result, result_sharded.full_tensor())


def test_replicate_and_col_wise_shard():
    backend = mithril.TorchBackend()
    backend_parallel = create_parallel_backend(device_mesh=(2, 2))

    input = backend.rand((32, 32))
    weight = backend.rand((32, 32))

    input_sharded = backend_parallel.array(input, device_mesh=((0, 1), (1, 2)))
    col_wise_weight = backend_parallel.array(weight, device_mesh=((0, 1), (1, 2)))
    assert col_wise_weight.placements == (Replicate(), Shard(1))
    assert col_wise_weight._local_tensor.shape == (32, 16)

    result = input @ weight
    result_sharded = input_sharded @ col_wise_weight
    assert torch.allclose(result, result_sharded.full_tensor())


def test_async_matmul_tensor(mocker):
    backend = mithril.TorchBackend()
    backend_parallel = create_parallel_backend(device_mesh=(1, 4))
    input = backend.rand((32, 32))
    row_wise_w = backend.rand((128, 32))
    col_wise_w = backend.rand((8, 32))

    input_shard = backend_parallel.array(input, device_mesh=((0, 1), (1, 4)))
    row_wise_w_shard = backend_parallel.array(row_wise_w, device_mesh=((0, 1), (0, 4)))
    col_wise_w_shard = backend_parallel.array(col_wise_w, device_mesh=((0, 1), (1, 4)))

    gather_patch = mocker.patch(
        "mithril.cores.python.torch.ops.async_gather_linear",
        wraps=mithril.cores.python.torch.ops.async_gather_linear,
    )
    scatter_patch = mocker.patch(
        "mithril.cores.python.torch.ops.async_scatter_linear",
        wraps=mithril.cores.python.torch.ops.async_scatter_linear,
    )

    result_row_wise = input @ row_wise_w.T
    result_sharded_row_wise = input_shard @ row_wise_w_shard.T
    assert gather_patch.called
    assert not scatter_patch.called

    gather_patch.reset_mock()
    scatter_patch.reset_mock()

    result_col_wise = input @ col_wise_w.T
    result_sharded_col_wise = input_shard @ col_wise_w_shard.T
    assert scatter_patch.called
    assert not gather_patch.called

    assert torch.allclose(result_row_wise, result_sharded_row_wise.full_tensor())
    assert torch.allclose(result_col_wise, result_sharded_col_wise.full_tensor())


def test_torch_static_parallel_1():
    # This test checks parallel execution with partial static inference.
    model = Model()
    model |= (linear := Linear(256))(input="input", weight="w", bias="b")
    model |= Sigmoid()(input=linear.output, output="output")
    backend = create_parallel_backend(device_mesh=(4, 1))

    static_inputs = {
        "input": backend.ones(256, 128, device_mesh=(4, 1)),
        "w": backend.ones([256, 128]),
    }
    pm = compile(model, backend, jit=False, constant_keys=static_inputs)

    params = {"b": backend.ones([256])}

    result = pm.evaluate(params)
    out = result["output"]
    assert isinstance(out, torch.distributed._tensor.DTensor)

    output_full_tensor = out.full_tensor()

    np.testing.assert_allclose(
        output_full_tensor, ((torch.ones(256, 256) * 129).sigmoid())
    )


def test_torch_static_parallel_2():
    # This test checks parallel execution with full static inference.
    model = Model()
    model |= (linear := Linear(256))(input="input", weight="w", bias="b")
    model |= Sigmoid()(input=linear.output, output="output")
    backend = create_parallel_backend(device_mesh=(4, 1))

    static_inputs = {
        "input": backend.ones(256, 128, device_mesh=(4, 1)),
        "w": backend.ones([256, 128]),
        "b": backend.ones([256]),
    }
    pm = compile(model, backend, jit=False, constant_keys=static_inputs, inference=True)

    result = pm.evaluate()
    out = result["output"]
    assert isinstance(out, torch.distributed._tensor.DTensor)

    output_full_tensor = out.full_tensor()
    np.testing.assert_allclose(
        output_full_tensor, ((torch.ones(256, 256) * 129).sigmoid())
    )


def test_torch_static_parallel_3():
    # This test checks parallel execution with full static inference.
    model = Model()
    model |= (linear := Linear(256))(input="input", weight="w", bias="b")
    model |= Relu()(input=linear.output, output=IOKey("output"))
    model |= Tanh()(input="input2", output=IOKey("output2"))
    backend = create_parallel_backend(device_mesh=(4, 1))

    static_inputs = {
        "input": backend.ones(256, 128, device_mesh=(4, 1)),
        "w": backend.ones([256, 128]),
        "b": backend.ones([256]),
        "input2": backend.ones((16, 16)),
    }
    pm = compile(model, backend, jit=False, constant_keys=static_inputs, inference=True)

    result = pm.evaluate()
    out1 = result["output"]
    out2 = result["output2"]
    assert isinstance(out1, torch.distributed._tensor.DTensor)
    assert isinstance(out2, torch.distributed._tensor.DTensor)

    output_full_tensor = out1.full_tensor()
    output2_full_tensor = out2.full_tensor()
    np.testing.assert_allclose(
        output_full_tensor, ((torch.ones(256, 256) * 129).relu())
    )
    np.testing.assert_allclose(output2_full_tensor, (torch.ones(16, 16).tanh()))


def test_torch_parallel_error_1():
    # This test checks if an error is raised when trying to create a Parallel object
    # with only one device.
    model = Model()
    model += Linear(256)(input="input", weight="w", bias="b")

    with pytest.raises(ValueError) as e:
        create_parallel_backend(device_mesh=(1,))

    assert str(e.value) == (
        "Provided '1' for n_devices, but parallel execution requires ndevices "
        "greater than 1."
    )


def test_torch_parallel_error_2():
    # This test checks if an error is raised when trying to shard a tensor with
    # incompatible dimensions.
    model = Model()
    model += Linear(256)(input="input", weight="w", bias="b")
    backend = create_parallel_backend(device_mesh=(2, 1))

    tensor = torch.ones(3, 128)
    with pytest.raises(ValueError) as e:
        backend.array(tensor, device_mesh=(2,))

    assert (
        str(e.value)
        == "Sharding requires all dimensions to be divisible by the device mesh dims."
    )


def test_torch_parallel_error_3():
    # User must provide device mesh
    model = Model()
    model += Linear(256)(input="input", weight="w", bias="b")
    backend = create_parallel_backend(device_mesh=(2, 1))

    with pytest.raises(ValueError) as e:
        backend.array(torch.ones(4, 4), device_mesh=(4, 1))

    assert str(e.value) == "Device mesh must be compatible with the model device mesh."


def test_torch_parallel_error_4():
    # User must provide device mesh
    model = Model()
    model += Linear(256)(input="input", weight="w", bias="b")
    backend = create_parallel_backend(device_mesh=(1, 2))

    with pytest.raises(ValueError) as e:
        backend.array(torch.ones(4, 4), device_mesh=(2, 1))

    assert str(e.value) == "Device mesh must be compatible with the model device mesh."


def test_torch_parallel_error_5():
    # User must provide device mesh
    model = Model()
    model += Linear(256)(input="input", weight="w", bias="b")
    backend = create_parallel_backend(device_mesh=(2, 1))

    tensor = torch.ones([8, 128])
    sharded_tensor = backend.array(tensor, device_mesh=(2,))
    assert sharded_tensor._local_tensor.shape == (4, 128)  # type: ignore
    assert sharded_tensor.shape == (8, 128)


def test_torch_parallel_error_6():
    # User must provide device mesh
    model = Model()
    model += Linear(256)(input="input", weight="w", bias="b")
    backend = create_parallel_backend(device_mesh=(2, 1))

    tensor = backend.ones([8, 128])
    with pytest.raises(AssertionError) as e:
        backend.array(tensor, device_mesh=(2, 1))

    assert str(e.value) == "shard_tensor expects a torch.Tensor, but got a STensor"


def test_torch_parallel_error_7():
    # In parallize, device mesh must have the same or less dimensions than the tensor
    model = Model()
    model += Linear(256)(input="input", weight="w", bias="b")
    backend = create_parallel_backend(device_mesh=(2, 1))

    tensor = torch.ones([8, 128])
    with pytest.raises(ValueError) as e:
        backend.array(tensor, device_mesh=(2, 1, 1))

    assert (
        str(e.value)
        == "Device mesh must have the same number of dimensions as the base mesh."
    )


def test_torch_parallel_error_8():
    # Shard only 1 dimension and replicate in others
    model = Model()
    model += Linear(256)(input="input", weight="w", bias="b")
    backend = create_parallel_backend(device_mesh=(2, 2))

    tensor = torch.ones([8, 128])
    sharded_tensor = backend.array(tensor, device_mesh=(2,))
    assert sharded_tensor._local_tensor.shape == (4, 128)  # type: ignore
    assert sharded_tensor.shape == (8, 128)


def test_torch_parallel_error_9():
    # Shard all dimensions
    model = Model()
    model += Linear(256)(input="input", weight="w", bias="b")
    backend = create_parallel_backend(device_mesh=(2, 2))

    tensor = torch.ones([8, 128])
    sharded_tensor = backend.array(tensor, device_mesh=(2, 2))
    assert sharded_tensor._local_tensor.shape == (4, 64)  # type: ignore
    assert sharded_tensor.shape == (8, 128)


def test_torch_parallel_error_10():
    # Replicate in first dimension and shard in others
    model = Model()
    model += Linear(256)(input="input", weight="w", bias="b")
    backend = create_parallel_backend(device_mesh=(2, 2))

    tensor = torch.ones([8, 128])
    sharded_tensor = backend.array(tensor, device_mesh=(1, 2))
    assert sharded_tensor._local_tensor.shape == (8, 64)  # type: ignore
    assert sharded_tensor.shape == (8, 128)


def test_torch_parallel_error_11():
    # Replicate all dimensions explicitly
    model = Model()
    model += Linear(256)(input="input", weight="w", bias="b")
    backend = create_parallel_backend(device_mesh=(2, 2))

    tensor = torch.ones([8, 128])
    sharded_tensor = backend.array(tensor, device_mesh=(1, 1))
    assert sharded_tensor._local_tensor.shape == (8, 128)  # type: ignore
    assert sharded_tensor.shape == (8, 128)


def test_torch_parallel_error_12():
    # Torch device mesh must be tuple or None
    model = Model()
    model += Linear(256)(input="input", weight="w", bias="b")
    with pytest.raises(AssertionError) as e:
        mithril.TorchBackend(device_mesh=2)  # type: ignore

    assert str(e.value) == "device_mesh must be a tuple or None."


def test_torch_parallel_multi_parallel_1():
    # Create multiple parallel backends
    backend1 = create_parallel_backend(device_mesh=(2, 1))
    backend2 = create_parallel_backend(device_mesh=(2, 1))
    model = Model()
    model += Add()(left=IOKey("left", type=Tensor), right=IOKey("right", type=Tensor))
    pm1 = compile(
        model,
        backend1,
        jit=False,
        shapes={"left": [8, 128], "right": [8, 128]},
        data_keys={"left", "right"},
        inference=True,
    )

    model = Model()
    model += Multiply()(
        left=IOKey("left", type=Tensor), right=IOKey("right", type=Tensor)
    )
    pm2 = compile(
        model,
        backend2,
        jit=False,
        shapes={"left": [8, 128], "right": [8, 128]},
        data_keys={"left", "right"},
        inference=True,
    )

    left = backend1.ones([8, 128]) * 5
    right = backend1.ones([8, 128]) * 5

    res1 = pm1.evaluate({}, {"left": left, "right": right})
    res2 = pm2.evaluate({}, {"left": left, "right": right})

    out1 = res1["output"]
    out2 = res2["output"]

    assert isinstance(out1, torch.distributed._tensor.DTensor)
    assert isinstance(out2, torch.distributed._tensor.DTensor)
    np.testing.assert_allclose(
        out1.full_tensor(),
        (left + right).full_tensor(),  # type: ignore
    )
    np.testing.assert_allclose(
        out2.full_tensor(),
        (left * right).full_tensor(),  # type: ignore
    )


def test_torch_parallel_multi_parallel_2():
    # Create multiple parallel backends with different mesh shapes
    backend1 = create_parallel_backend(device_mesh=(2, 1))
    backend2 = create_parallel_backend(device_mesh=(1, 2))

    tensor1 = backend1.ones([8, 128], device_mesh=(2, 1)) * 5
    tensor2 = backend2.ones([8, 128], device_mesh=(1, 2)) * 5

    assert tensor1._local_tensor.shape == (4, 128)  # type: ignore
    assert tensor2._local_tensor.shape == (8, 64)  # type: ignore

    tensor3 = backend1.ones([8, 128]) * 5
    tensor4 = backend2.ones([8, 128]) * 5

    assert tensor3._local_tensor.shape == (8, 128)  # type: ignore
    assert tensor4._local_tensor.shape == (8, 128)  # type: ignore


def test_torch_parallel_multi_parallel_3():
    # Create multiple parallel backends with different incompatible mesh shapes
    if TorchParallel._instance is not None:
        TorchParallel._instance.clean_up()

    mithril.TorchBackend(device_mesh=(2, 1))
    with pytest.raises(AssertionError) as e:
        mithril.TorchBackend(device_mesh=(2, 2))

    assert str(e.value) == (
        "TorchParallel is initialized already with n_devices=2. Cannot "
        "reinitialize with n_devices=4"
    )

    if TorchParallel._instance is not None:
        TorchParallel._instance.clean_up()


# @pytest.mark.skip(reason="Only works in cuda devices")
def test_jax_parallel_1():
    if "cuda" in mithril.JaxBackend.get_available_devices():
        model = Model()
        model += Linear(256)(input="input", weight="w", bias="b")
        backend = mithril.JaxBackend(device="cuda")
        backend_parallel = mithril.JaxBackend(device="cuda", device_mesh=(4,))

        tensor1 = backend.ones(8, 128)

        pm = compile(
            model,
            backend,
            jit=True,
            shapes={"input": [8, 128]},
            data_keys={"input"},
        )
        pm_parallel = compile(
            model,
            backend_parallel,
            jit=True,
            shapes={"input": [8, 128]},
            data_keys={"input"},
        )

        sharded_tensor = backend_parallel.array(tensor1, device_mesh=(4,))
        replicated_tensor = backend_parallel.array(tensor1)

        assert sharded_tensor.sharding.shape == (4, 1)  # type: ignore
        assert replicated_tensor.sharding.shape == (1, 1)  # type: ignore

        # Apply op to sharded tensor
        sharded_tensor += 1
        np.testing.assert_allclose(sharded_tensor, (jax.numpy.ones((8, 128)) + 1))

        result_tensor = replicated_tensor + sharded_tensor
        assert result_tensor.sharding.shape == (4, 1)  # type: ignore

        np.testing.assert_allclose(result_tensor, (jax.numpy.ones((8, 128)) + 2))

        params = pm.randomize_params()

        params = {key: backend.ones(value.shape) for key, value in params.items()}
        input = {"input": backend.array(tensor1)}

        params_parallel = {
            key: backend_parallel.ones(value.shape) for key, value in params.items()
        }
        input_parallel = {"input": backend_parallel.array(tensor1, device_mesh=(4,))}

        result_parallel = pm_parallel.evaluate(params_parallel, input_parallel)
        out = result_parallel["output"]
        assert isinstance(out, jax.Array)

        output_full_tensor = result_parallel["output"]
        assert isinstance(output_full_tensor, jax.numpy.ndarray)
        np.testing.assert_allclose(output_full_tensor, (jax.numpy.ones((8, 256)) * 129))

        output_grad = backend.randn(8, 256)
        output_grad_parallel = backend_parallel.array(output_grad)
        _, grads = pm.evaluate(params, input, output_gradients={"output": output_grad})
        _, grads_parallel = pm_parallel.evaluate(
            params_parallel,
            input_parallel,
            output_gradients={"output": output_grad_parallel},
        )

        for _key, _grad in grads.items():
            parallel_grad = grads_parallel[_key]
        np.testing.assert_allclose(_grad, parallel_grad, 1e-5, 1e-5)


def test_jax_parallel_2():
    # This test checks parallel execution with a model that includes array creation
    # primitive eye.
    if "cuda" in mithril.JaxBackend.get_available_devices():
        model = Model()
        model += (linear := Linear(256))(input="input", weight="w", bias="b")
        model += (e := Eye(N=TBD))(N=linear.output.shape[0])
        model += Add()(left=linear.output, right=e.output, output="output")
        backend = mithril.JaxBackend(device="cuda", device_mesh=(4, 1))
        backend.ones([256])
        pm = compile(model, backend, jit=False, data_keys={"input"})

        params = {"w": backend.ones([128, 256]), "b": backend.ones([256])}

        # Replicate params
        input = {"input": backend.ones(256, 128, device_mesh=(4, 1))}
        result = pm.evaluate(params, input)

        output_full_tensor = result["output"]
        assert isinstance(output_full_tensor, jax.numpy.ndarray)
        np.testing.assert_allclose(
            output_full_tensor, (jax.numpy.ones((256, 256)) * 129 + jax.numpy.eye(256))
        )


def test_jax_parallel_3():
    # This test checks parallel execution with a model that includes array creation
    # primitive to_tensor.
    if "cuda" in mithril.JaxBackend.get_available_devices():
        model = Model()
        model += (linear := Linear(256))(input="input", weight="w", bias="b")
        model += (e := ToTensor())(input=IOKey("to_tensor", value=TBD))
        model += Add()(left=linear.output, right=e.output, output="output")
        backend = mithril.JaxBackend(device="cuda")
        backend_parallel = mithril.JaxBackend(device="cuda", device_mesh=(4, 1))

        pm = compile(model, backend, jit=False, data_keys={"input", "to_tensor"})
        pm_parallel = compile(
            model,
            backend_parallel,
            jit=False,
            data_keys={"input", "to_tensor"},
        )

        params = {"w": backend.ones([128, 256]), "b": backend.ones([256])}
        params_parallel = {
            "w": backend_parallel.ones([128, 256]),
            "b": backend_parallel.ones([256]),
        }

        input: dict[str, jax.Array | list[int]] = {
            "input": backend.ones(256, 128),
            "to_tensor": [1, 2, 3, 4] * 64,
        }
        input_parallel: dict[str, jax.Array | list[int]] = {
            "input": backend_parallel.ones(256, 128, device_mesh=(4, 1)),
            "to_tensor": [1, 2, 3, 4] * 64,
        }

        result_parallel = pm_parallel.evaluate(params_parallel, input_parallel)
        output_full_tensor = result_parallel["output"]
        assert isinstance(output_full_tensor, jax.numpy.ndarray)
        np.testing.assert_allclose(
            output_full_tensor,
            (
                jax.numpy.ones((256, 256)) * 129
                + (jax.numpy.tile(jax.numpy.arange(4), 64) + 1)
            ),
        )

        output_grad = backend.randn(256, 256)
        output_grad_parallel = backend_parallel.array(output_grad)
        _, grads = pm.evaluate(params, input, output_gradients={"output": output_grad})
        _, grads_parallel = pm_parallel.evaluate(
            params_parallel,
            input_parallel,
            output_gradients={"output": output_grad_parallel},
        )

        for key, grad in grads.items():
            parallel_grad = grads_parallel[key]
            np.testing.assert_allclose(grad, parallel_grad, 1e-5, 1e-5)


def test_jax_parallel_4():
    # This test checks parallel execution with a model that includes immediate values in
    #  Add primitive.
    if "cuda" in mithril.JaxBackend.get_available_devices():
        model = Model()
        model += (linear := Linear(256))(
            input="input", weight="w", bias="b", output="out1"
        )
        model += Add()(left=linear.output, right=Tensor([3] * 256), output="output")

        backend = mithril.JaxBackend("cuda")
        backend_parallel = mithril.JaxBackend("cuda", device_mesh=(4, 1))

        pm = compile(model, backend, jit=False, data_keys={"input"})
        pm_parallel = compile(model, backend_parallel, jit=False, data_keys={"input"})

        params = {"w": backend.ones([128, 256]), "b": backend.ones([256])}
        input = {"input": backend.ones(256, 128)}
        params_parallel = {
            "w": backend_parallel.ones([128, 256]),
            "b": backend_parallel.ones([256]),
        }
        input_parallel = {"input": backend_parallel.ones(256, 128, device_mesh=(4, 1))}

        result_parallel = pm_parallel.evaluate(params_parallel, input_parallel)
        output_full_tensor = result_parallel["output"]
        assert isinstance(output_full_tensor, jax.numpy.ndarray)
        np.testing.assert_allclose(
            output_full_tensor, jax.numpy.ones((256, 256)) * 129 + 3
        )

        output_grad = backend.randn(256, 256)
        output_grad_parallel = backend_parallel.array(output_grad)
        _, grads = pm.evaluate(params, input, output_gradients={"output": output_grad})
        _, grads_parallel = pm_parallel.evaluate(
            params_parallel,
            input_parallel,
            output_gradients={"output": output_grad_parallel},
        )

        for key, grad in grads.items():
            parallel_grad = grads_parallel[key]
            np.testing.assert_allclose(grad, parallel_grad, 1e-5, 1e-5)


def test_jax_parallel_5():
    # This test checks parallel execution with a model that includes cache of
    # primitive eye.
    if "cuda" in mithril.JaxBackend.get_available_devices():
        model = Model()
        model += (linear := Linear(256))(input="input", weight="w", bias="b")
        model += (e := Eye(N=TBD))(N=linear.output.shape[0])
        model += Add()(left=linear.output, right=e.output, output="output")

        backend = mithril.JaxBackend(device="cuda")
        backend_parallel = mithril.JaxBackend(device="cuda", device_mesh=(2, 2))
        pm = compile(
            model,
            backend,
            jit=False,
            shapes={"input": [256, 128]},
            data_keys={"input"},
        )
        pm_parallel = compile(
            model,
            backend_parallel,
            jit=False,
            shapes={"input": [256, 128]},
            data_keys={"input"},
        )

        input = {"input": backend.ones(256, 128)}
        params = {"w": backend.ones([128, 256]), "b": backend.ones([256])}
        input_parallel = {"input": backend_parallel.ones(256, 128, device_mesh=(2, 2))}
        params_parallel = {
            "w": backend_parallel.ones([128, 256]),
            "b": backend_parallel.ones([256]),
        }

        result = pm.evaluate(params, input)
        result_parallel = pm_parallel.evaluate(params_parallel, input_parallel)

        output_grads = backend.randn(256, 256)
        outout_grads_parallel = backend_parallel.array(output_grads)
        output_full_tensor = result_parallel["output"]
        out = result["output"]
        assert isinstance(out, jax.numpy.ndarray)
        assert isinstance(output_full_tensor, jax.numpy.ndarray)
        np.testing.assert_allclose(output_full_tensor, out)

        _, param_grads = pm.evaluate(
            params, input, output_gradients={"output": output_grads}
        )
        _, param_grads_parallel = pm_parallel.evaluate(
            params_parallel,
            input_parallel,
            output_gradients={"output": outout_grads_parallel},
        )
        for key, grad in param_grads.items():
            parallel_grad = param_grads_parallel[key]
            np.testing.assert_allclose(grad, parallel_grad, 1e-5, 1e-5)
