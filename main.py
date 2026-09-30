import os
from datahandler import DataHandler
from metric import Metric

from graphlib import TopologicalSorter, CycleError
from datetime import datetime, timedelta, timezone
import time
import logging
import sys
import argparse
from utils.memory_tracker import memusage, rss_memusage
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

log = logging.getLogger(__name__)

def main_loop_iteration(datahandler : DataHandler, metrics : list[Metric], from_timestamp : datetime, to_timestamp : datetime, log_mem: bool, less_entities: bool):
    t0 = time.perf_counter()
    t0p = time.process_time()
    datahandler.load_next_timestep(from_timestamp, to_timestamp, less_entities)
    log.info("Loading data (%s -> %s) done in %.2fs (real) and %.2fs (process)", from_timestamp, to_timestamp, time.perf_counter() - t0, time.process_time() - t0p) # real time
    log.info("Memory usage (start of timestep, peak): %s", memusage())
    log.info("Memory usage (start of timestep, current): %s", rss_memusage())
    log.info("Calculating metrics")
    #1. top sort metrics based on dependencies -> the same every timestep but likely low overhead compared to other steps

    dependencies = {metric: metric.get_dependencies() for metric in metrics}
    ts = TopologicalSorter(dependencies)
    try:
        ts.prepare()
    except CycleError as ce:
        log.info(f"Cycle detected in metric dependencies: {ce}")
        return {}
    
    # store (real_seconds, cpu_seconds, peak_memory_mb)
    times: dict[str, list[tuple[float, float]]] = {m.metric_name: [] for m in metrics}

    while ts.is_active():
        parallel_metric_batch = ts.get_ready()
        #2. process metrics in sliced order (TODO: can be multiprocessed)
        for metric in parallel_metric_batch:
            log.info(f"Calculating metric: {metric.metric_name}")
            start_time = time.perf_counter()
            start_time_cpu = time.process_time()

            metric.calculate()
            
            if log_mem:
                metric.log_memory()

            end_time = time.perf_counter()
            end_time_cpu = time.process_time()

            real_duration = end_time - start_time # real amount of time
            cpu_duration = end_time_cpu - start_time_cpu  # time spent by the computer for the current process

            times[metric.metric_name].append((real_duration, cpu_duration))
            ts.done(metric)
    log.info("Memory usage (end of timestep, peak): %s", memusage())
    log.info("Memory usage (end of timestep, current): %s", rss_memusage())
    
    return times


def main(log_mem, less_entities, use_cache):
    datahandler: DataHandler = DataHandler(wipe_metrics_table=False)
    
    number_of_steps : int = 2
    if use_cache:
        initial_time, timestep_length = datahandler.get_timestep_config()
    else:
        initial_time : datetime = datetime(2012, 10, 29, 0, 0, tzinfo=timezone.utc) #Wikidata was created on the 29th of October 2012
        timestep_length : timedelta = timedelta(days = 30) #30
    start_time = initial_time
    
    log.info("starting")
    log.info(f"Memory logging: {log_mem}")
    log.info(f"Less entities: {less_entities}")
    if less_entities:
        log.warning("less_entities is currently not supported by the metric builder")
    log.info(f"Use cache: {use_cache}")

    from metric_builder import build_metrics
    metrics : list[Metric] = build_metrics(datahandler, use_cache=use_cache)
    
    print("Build metrics")
    datahandler.reset_cache()
    
    # total times across all timesteps
    times: dict[str, list[tuple[float, float]]] = {m.metric_name: [] for m in metrics}

    import tqdm
    for i in tqdm.tqdm(range(number_of_steps), desc="Timesteps"):
        end_time = start_time + timestep_length
        log.info("Start timestep %s -> %s", start_time, end_time)

        t0 = time.perf_counter()
        t0p = time.process_time()

        iteration_times = main_loop_iteration(datahandler, metrics, start_time, end_time, log_mem, less_entities)
        for metric_name, time_ in iteration_times.items():
            real, cpu = time_[0]
            log.info("(Iteration %i) %s: real %.2fs | cpu %.2fs", i, metric_name, real, cpu)

        log.info("Iteration %i finished in %.2fs (real) and %.2fs (process)", i, time.perf_counter() - t0, time.process_time() - t0p)

        for key, durations in iteration_times.items():
            times[key].extend(durations)

        start_time = end_time
    
    for metric in metrics:
        metric.save_cache()
    datahandler.save_timestep_config(end_time, timestep_length)
    
    log.info("finished")
    # Aggregate properly
    totals = []
    for key, durations in times.items():
        total_real = sum(d[0] for d in durations)
        total_cpu = sum(d[1] for d in durations)
        totals.append((key, total_real, total_cpu))

    totals.sort(key=lambda x: x[1])  # sort by total real time

    for key, total_real, total_cpu in totals:
        log.info("TOTAL %s: real %.2fs | cpu %.2fs", key, total_real, total_cpu)

    print("\n--- Metric Name to ID Mapping ---")
    for name, metric_id in datahandler.metric_name_to_id.items():
        print(f"'{name}': {metric_id}")
    print("-------------------------------------\n")

    print("Run ID:", datahandler.run_id)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("--log_mem", default='0')
    parser.add_argument("--less_entities", default='0')
    parser.add_argument("--use_cache", default='0')

    args = parser.parse_args()
    
    log_mem = args.log_mem == '1'
    less_entities = args.less_entities == '1'
    use_cache = args.use_cache == '1'
    main(log_mem, less_entities, use_cache)




    
