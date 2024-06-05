import os
import sys
import tempfile
import time
import numpy
import torch
import torch.distributed
import torch.optim as optim
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data import IterableDataset, DataLoader
from pickle import load, dump
from copy import deepcopy
from typing import Callable, Type, Union, Iterable, Any
from tqdm import tqdm

if __name__ == '__main__':
    sys.path.append('..')
from utilities.training import train, confusion
from utilities import *

_directory = os.path.dirname(os.path.abspath(__file__))

def setup(rank, world_size):
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12355'
    torch.distributed.init_process_group("nccl", rank=rank, world_size=world_size)
def cleanup():
    torch.distributed.destroy_process_group()

class _aims:
    train:int
    confusion:int
    execute:int
    def __init__(self):
        self.train = 1
        self.confusion = 1
        self.execute = 1
aims = _aims()



def _train_flow(rank:int, world_size:int, model:torch.nn.Module, dataset:Dataset, loss_function:Callable[[torch.Tensor,torch.Tensor],torch.Tensor], optimizer:Type[torch.optim.Optimizer], optimizer_args, optimizer_kwargs):
    print(f"Training thread#{rank} PID is: {os.getpid()}")
    setup(rank, world_size)
    model = torch.nn.parallel.DistributedDataParallel(deepcopy(model).to(rank))
    dataset.sampler.parallel()
    optimizer = optimizer(model.parameters(), *optimizer_args, **optimizer_kwargs)
    loss_array = train(model, dataset, optimizer, loss_function, echo=(rank == 0))

    loss_arrays_list = [None for _ in range(world_size)]
    torch.distributed.gather_object(loss_array, loss_arrays_list, dst=0)

    torch.distributed.barrier()
    if rank == 0:
        loss_array_reduced = sum(loss_arrays_list) / world_size
        with open(_directory + "/cash/results.pkl", 'wb') as file:
            model = model.module.cpu()
            dump((model, loss_array_reduced), file)
    cleanup()
def _train(model:torch.nn.Module, dataset:Dataset, loss_function:Callable[[torch.Tensor,torch.Tensor],torch.Tensor], optimizer:Type[torch.optim.Optimizer], *optimizer_args, **optimizer_kwargs):
    world_size = torch.cuda.device_count()
    torch.multiprocessing.spawn(_train_flow, args=(world_size, model, dataset, loss_function, optimizer, optimizer_args, optimizer_kwargs), nprocs=world_size, join=True)


def _confusion_flow(rank:int, world_size:int, model:torch.nn.Module, dataset:Dataset, classes:int):
    print(f"Confusion thread#{rank} PID is: {os.getpid()}")
    setup(rank, world_size)
    model = torch.nn.parallel.DistributedDataParallel(deepcopy(model).to(rank))
    dataset.sampler.parallel()

    confusion_matrix = confusion(model, dataset, classes, echo=(rank == 0))

    confusion_matrices_list = [None for _ in range(world_size)]
    torch.distributed.gather_object(confusion_matrix, confusion_matrices_list, dst=0)

    torch.distributed.barrier()
    if rank == 0:
        confusion_matrix_reduced = sum(confusion_matrices_list) / world_size
        with open(_directory + "/cash/results.pkl", 'wb') as file:
            dump(confusion_matrix_reduced, file)
    cleanup()
def _confusion(model:torch.nn.Module, dataset:Dataset, classes:int):
    world_size = torch.cuda.device_count()
    torch.multiprocessing.spawn(_confusion_flow, args=(world_size, model, dataset, classes), nprocs=world_size, join=True)


def _execute_flow(rank:int, world_size:int, model:torch.nn.Module, dataset:DataLoader, extract:Union[Callable[[torch.Tensor],Any],Callable[[torch.Tensor,torch.Tensor],Any]], _type:bool):
    print(f"Confusion thread#{rank} PID is: {os.getpid()}")
    setup(rank, world_size)
    model = torch.nn.parallel.DistributedDataParallel(deepcopy(model).to(rank))

    results = []
    idx_offset = rank * len(dataset) // world_size
    iterator = tqdm(dataset, disable=rank != 0)
    if _type:
        for idx, (data, correct) in enumerate(iterator, start=idx_offset):
            data:torch.Tensor
            data = data.to(rank)
            correct = correct.to(rank)

            result = model.forward(data)
            result = extract(result, correct)
            results.append((idx, result))
    else:
        for idx, data in enumerate(iterator, start=idx_offset):
            data = data.to(rank)

            result = model.forward(data)
            result = extract(result)
            results.append((idx, result))

    results_list:list = [None for _ in range(world_size)]
    torch.distributed.gather_object(results, results_list, dst=0)

    torch.distributed.barrier()
    if rank == 0:
        results_list:list
        results:list = sum(results_list)
        results.sort(key=lambda x: x[0])
        results = [item[1] for item in results]
        with open(_directory + "/cash/results.pkl", 'wb') as file:
            dump(results, file)
    cleanup()
def _execute(model:torch.nn.Module, data:Union[Dataset, Iterable[torch.Tensor]], extract:Union[Callable[[torch.Tensor],Any],Callable[[torch.Tensor,torch.Tensor],Any]]):
    if isinstance(data, Dataset):
        data.sampler.parallel()
        dataloader = data.test
        _type = True
    else:
        class TempDataset(IterableDataset):
            _data:Iterable[torch.Tensor]
            def __init__(self, _data:Iterable[torch.Tensor]):
                self._data = _data
            def __iter__(self):
                for item in self._data:
                    yield item
        dataset = TempDataset(data)
        dataloader = DataLoader(dataset, sampler=DistributedSampler(dataset))
        _type = False
    world_size = torch.cuda.device_count()
    torch.multiprocessing.spawn(_confusion_flow, args=(world_size, model, dataloader, extract, _type), nprocs=world_size, join=True)


def main():
    aim = int(sys.argv[1])
    with open(_directory + "/cash/arguments.pkl", 'rb') as file:
        arguments = load(file)
    if aim == aims.train:
        _train(*arguments)
    elif aim == aims.confusion:
        _confusion(*arguments)
    elif aim == aims.execute:
        _execute(*arguments)
    elif aim == -1:
        print("Test cluster run execution")
        for i in range(7):
            print(i)
            time.sleep(2)
    else:
        raise AttributeError('Выбран не верный тип операции')
if __name__ == '__main__':
    main()