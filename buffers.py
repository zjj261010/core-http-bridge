"""Bound PTY buffering with backpressure instead of dropping terminal bytes."""
import asyncio

class PtyQueue(asyncio.Queue):
    def __init__(self, loop, fd):
        super().__init__(maxsize=128)
        self.loop,self.fd,self.callback=loop,fd,None
        self.paused=False
    def put_nowait(self,item):
        super().put_nowait(item)
        if self.full() and self.callback:
            self.loop.remove_reader(self.fd)
            self.paused=True
    async def get(self):
        item=await super().get()
        if self.paused and self.qsize()<64:
            self.loop.add_reader(self.fd,self.callback)
            self.paused=False
        return item
