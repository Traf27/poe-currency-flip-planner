import asyncio
import logging
from typing import Any, Dict, List, Tuple

import aiohttp
from asyncio_throttle import Throttler

from src.core.offer import Offer
from src.core.backends.poeofficial import PoeOfficial
from src.core.backends.poetrade import PoeTrade
from src.core.backends.task import Task
from src.trading import ItemList


class BackendPoolWorker:
    backend: Any
    loop: asyncio.AbstractEventLoop
    rate_limiter: Throttler
    results: List[Any]
    just_failed: bool
    work_index: Dict[int, Task]

    def __init__(self, backend: Any, loop: asyncio.AbstractEventLoop,
                 rate_limiter: Throttler):
        self.backend = backend
        self.loop = loop
        self.rate_limiter = rate_limiter
        self.results = []
        self.counter = 0
        self.just_failed = False
        self.work_index = dict()

    def pick_tasks(self, queue: asyncio.Queue, n_tasks: int) -> List[Task]:
        # TODO worker must support the items in the task in order to work on it
        # need some additional item_list checks and some asyncio.Queue wrapper
        # that makes sure task distribution happens correctly
        tasks: List[Task] = []

        for i in range(n_tasks):
            try:
                task: Task = queue.get_nowait()
                tasks.append(task)
            except asyncio.QueueEmpty:
                break

        return tasks

    async def handle_error(self):
        if self.just_failed is True:
            logging.debug("Backend {} failed: Time penalty...".format(
                self.backend.name()))
            await asyncio.sleep(15)
            self.just_failed = False

    async def work(self, queue: asyncio.Queue) -> List[Any]:
        client_session = aiohttp.ClientSession()

        while not queue.empty():
            await self.rate_limiter.acquire()

            tasks = self.pick_tasks(queue, self.rate_limiter.rate_limit)

            futures = []
            for i, task in enumerate(tasks):
                future = self.backend.fetch_offer_async(client_session, task)
                futures.append(future)
                self.work_index[i] = task
                self.counter = self.counter + 1

            done = await asyncio.gather(*futures, return_exceptions=True)
            for idx, result in enumerate(done):
                if isinstance(result, Exception):
                    failed_task = self.work_index[idx]
                    logging.debug("{}: Reschedule task: {} -> {}".format(
                        self.backend.name(), failed_task.have,
                        failed_task.want))
                    logging.debug(result)
                    queue.put_nowait(failed_task)
                    self.counter = self.counter - 1
                    self.just_failed = True
                else:
                    self.results.extend(result)

            self.work_index.clear()

            await self.handle_error()

        await client_session.close()

        return self.results


class BackendPool:
    backends: List[BackendPoolWorker]
    item_list: ItemList
    queue: asyncio.Queue
    event_loop: asyncio.AbstractEventLoop

    def __init__(self, item_list: ItemList):
        self.queue = asyncio.Queue()
        self.event_loop = asyncio.get_event_loop()
        self.item_list = item_list
        self.backends = [
            BackendPoolWorker(PoeTrade(item_list), self.event_loop,
                              Throttler(10, 1)),
            BackendPoolWorker(PoeOfficial(item_list), self.event_loop,
                              Throttler(2, 3)),
        ]

    def schedule(self,
                 league: str,
                 item_pairs: List[Tuple[str, str]],
                 item_list: ItemList,
                 limit: int = 10) -> List[Offer]:

        for p in item_pairs:
            new_task = Task(league, p[0], p[1], limit, False)
            self.queue.put_nowait(new_task)

        coroutines = [backend.work(self.queue) for backend in self.backends]

        (done, _pending) = self.event_loop.run_until_complete(
            asyncio.wait(coroutines))
        results: List[List[Dict]] = [x.result() for x in done]

        for worker in self.backends:
            logging.debug("Worker {} finished {} tasks".format(
                worker.backend.name(), worker.counter))

        # Restructure list of lists to a single list of offer dicts
        derp: List[Offer] = []
        for r in results:
            derp.extend(r)

        return derp
