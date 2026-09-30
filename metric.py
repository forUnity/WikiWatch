from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any #abstract base class

from datahandler import DataHandler

class Metric(ABC):
    datahandler : DataHandler
    metric_name : str
    cache: Any
    dependencies: list[Metric]

    # TODO: maybe add
    # version : int = 1

    @abstractmethod
    def calculate_diff(self) -> Any:
        pass
        #read changes for the current timestep from dataloader and compute diff value

    @abstractmethod
    def calculate(self):
        pass
        #wrapper that calls calculate_diff, fetches previous value, computes final value for this timestep and writes it back to cache

    @abstractmethod
    def write_result(self):
        pass
        #either write the diff, or write final value
        #NOTE: note sure we need this structure
        
    @abstractmethod
    def save_cache(self):
        pass
            
    @abstractmethod
    def log_memory(self):
        pass

    def get_last_value(self) -> Any:
        #get last value of this metric (for own computations and dependant metrics)
        #NOTE: for now get from own cache. If that does not fit in memory for all metrics we compute in parallel, then we could implement a database query from here. However, then there will be issues with not having an instance of this metric alive.
        return self.cache

    def get_dependencies(self) -> list[Metric]:
        return self.dependencies
