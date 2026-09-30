import sys
import multiprocessing as mp
import time
import queue
from lxml import etree
import os
import random

from dump_parsing_scripts.db_writer import db_writer
from dump_parsing_scripts.page_parser import PageParser
from dump_parsing_scripts.const import *


def process_page_xml(page_elem_str, file_name, config):
    parser = PageParser(file_name=file_name, page_elem_str=page_elem_str, config=config)
    try:
        return parser.process_page()
    except Exception as e:
        print(f"Error: Parser failed to parse page in file " + str(file_name))
        page_debug_log_directory: str | None = config.get("page_debug_log_directory")
        if page_debug_log_directory:
            randint = random.randint(10000, 99999)
            debug_page_path = os.path.join(page_debug_log_directory, f"{file_name}_{str(randint)}_error.page")
            debug_log_path = os.path.join(page_debug_log_directory, f"{file_name}_{str(randint)}_error.log")
            with open(debug_page_path, "w") as f:
                f.write(page_elem_str)
            with open(debug_log_path, "w") as f:
                f.write(str(e))
            print("Saved page content of failed page to " + debug_log_path)
    return None

class DumpParser():
    def __init__(self, file_name, config: dict):
       
        self.config: dict = config
        self.file_name = file_name
        self.num_entities = 0  
        
        self.num_workers = config.get('pages_in_parallel', 6) # processes that process pages in parallel
        self.page_queue = mp.Queue(maxsize=QUEUE_SIZE) # queue that stores pages as they are read
        self.stop_event = mp.Event()

        print(f"Starting db_writer expecting {self.num_workers} workers")
        
        # Create shared queue
        self.results_queue = mp.Manager().Queue(maxsize=QUEUE_SIZE)
        
        # Start shared db_writer
        self.db_writer_process = mp.Process(
            target=db_writer,
            args=(self.num_workers, self.results_queue)
        )
        self.db_writer_process.start()
        
        # TODO: remove
        self.start_time = time.time()

        self.workers = []
        for i in range(self.num_workers):
            p = mp.Process(target=self._worker, args=(i,))
            p.start()
            self.workers.append(p)
            
    def get_simple_stats(self):
        runtime = time.time() - self.start_time
        
        stats = {
            'runtime': runtime,
            'entities_processed': self.num_entities,
            'num_workers': self.num_workers,
        }
            
        return stats

    def _worker(self, worker_id):
        """
            Process started in init
            Gets pages from queue and calls process_page_xml which processes the page (entity)
        """
        pages_processed = 0
        total_wait_time = 0
        total_process_time = 0
        while not self.stop_event.is_set() or not self.page_queue.empty():
            wait_start = time.time()
            try:
                page_elem_str = self.page_queue.get(timeout=1) # get is atomic -  only one thread can remove an item at a time
                
                # ---- stats ----
                wait_time = time.time() - wait_start
                total_wait_time += wait_time
                # ---- stats ----
                
                if page_elem_str is None:  # no more pages to process
                    break
                
                process_start = time.time()
                results = process_page_xml(page_elem_str, self.file_name, self.config)
                if results is not None:
                    self.results_queue.put(results)
                
                # ---- stats ----
                process_time = time.time() - process_start
                total_process_time += process_time
                pages_processed += 1
                # ---- stats ----

                sys.stdout.flush()
            except queue.Empty:
                total_wait_time += time.time() - wait_start
                continue

        self.results_queue.put(None)

        # ---- stats ----
        total_time = total_process_time + total_wait_time
        efficiency = (total_process_time / total_time) * 100 if total_time > 0 else 0
        print(f"Worker {worker_id} finished: {pages_processed} pages, {efficiency:.1f}% efficiency, {total_process_time:.2f}s processing, {total_wait_time:.2f}s waiting")
        sys.stdout.flush()
        # ---- stats ----

    def parse_dump(self, file_obj):
        """
            Reads XML file and extracts pages of entities (title = Q-id).
            Each page is stored in a queue which is accessed by processes in parallel that extract the changes from the revisions
        """

        ns = "http://www.mediawiki.org/xml/export-0.11/"
        page_tag = f"{{{ns}}}page"
        title_tag = f"{{{ns}}}title"

        context = etree.iterparse(file_obj, events=("end",), tag=page_tag) # streams the file, doesn't load everything to memory
        
        last_report = time.time()

        for event, page_elem in context:
            keep = False
            entity_id = ""

            # Get title
            title_elem = page_elem.find(title_tag)
            if title_elem is not None:
                entity_id = title_elem.text or ""
                if entity_id.startswith("Q"):
                    keep = True

            if keep:
                # Serialize the page element
                page_elem_str = etree.tostring(page_elem, encoding="unicode")
                self.page_queue.put(page_elem_str)
                self.num_entities += 1

            # Periodic progress report
            if time.time() - last_report > 60:  # Every 1 min
                rate = self.num_entities / (time.time() - self.start_time)
                queue_size = self.page_queue.qsize()
                print(f"Progress: {self.num_entities} entities read, {rate:.1f} entities/sec, queue: {queue_size}/{QUEUE_SIZE}")
                sys.stdout.flush()
                last_report = time.time()

            # Clear page element to free memory
            page_elem.clear()
            while page_elem.getprevious() is not None:
                del page_elem.getparent()[0]

            if self.stop_event.is_set():
                break
        
        # Send stop signals to workers
        for _ in range(self.num_workers):
            self.page_queue.put(None)

        # Wait for workers to finish
        for i, p in enumerate(self.workers):
            p.join()

        self.stop_event.set()

        print("Waiting for db_writer to finish")
        self.db_writer_process.join()
        if self.db_writer_process.exitcode != 0:
            raise Exception(f"DB writer failed!")

        final_stats = self.get_simple_stats()
        print(f"\n=== FINAL STATISTICS ===")
        print(f"Total runtime: {final_stats['runtime']:.1f}s")
        print(f"Total entities processed: {self.num_entities}")
        print(f"Average processing rate: {self.num_entities/final_stats['runtime']:.2f} entities/sec")
        print(f"Workers used: {final_stats['num_workers']}")
        
        sys.stdout.flush()
