import atexit
from multiprocessing import Process, Queue as ProcessQueue
from queue import Queue as ThreadQueue  # TODO: change to asyncio.Queue for async
from threading import Thread
from base64 import b64decode
from io import BytesIO
from typing import Iterator, Type

from ..._ast import GrammarNode, ImageBlob, LiteralNode, RoleEnd, RoleStart
from ...trace import ImageOutput, OutputAttr, TextOutput
from .._base import Client
from ._engine import Engine
from ._state import EngineState
from ...trace import CaptureOutput

shutdown_none = True

def set_shutdown_flag():
    global shutdown_none
    # python can set anything to None at shutdown, so use None
    shutdown_none = None

atexit.register(set_shutdown_flag)

class EngineClient(Client[EngineState]):
    def __init__(self, engine_type: Type[Engine], *args, **kwargs):
        self._response_queue = ProcessQueue()
        self._work_queue = ProcessQueue()
        self._message_id = 2

        initial_thread_queue = ThreadQueue()
        
        self._outstanding = {1: initial_thread_queue}
        self._work_queue.put((1, "init", engine_type, args, kwargs))

        self._thread = Thread(target=local_worker, args=(self._outstanding, self._response_queue), daemon=False)
        self._thread.start()

        self._process = Process(target=remote_worker, args=(self._work_queue, self._response_queue), daemon=False)
        self._process.start()

        # wait for response that the engine has been created in the other process
        exception, *response_args = initial_thread_queue.get()
        if exception is not None:
            raise exception

        self.chat_template = response_args[0]

        # self.engine = engine_type(*args, **kwargs)
        # self.chat_template = self.engine.get_chat_template()

    def __del__(self):
        if shutdown_none is not None:
            call_close = getattr(self, "close", None)
            if callable(call_close):
                call_close()

    def close(self):
        p = getattr(self, "_process", None)
        if p is not None:
            work_queue = getattr(self, "_work_queue", None)
            if work_queue is not None:
                work_queue.put((None, "close"))
                p.join(30)
                self._work_queue = None

            if p.is_alive():
                p.terminate()
                p.join(30)

            p.close()
    
            self._process = None

        t = getattr(self, "_thread", None)
        if t is not None:
            response_queue = getattr(self, "_response_queue", None)
            if response_queue is not None:
                response_queue.put((0, None))
                self._response_queue = None

            t.join(30)  # max 30 seconds wait

            self._thread = None

    def get_role_start(self, role: str) -> str:
        if self.chat_template is None:
            raise ValueError("Cannot use roles without a chat template")
        return self.chat_template.get_role_start(role)

    def get_role_end(self, role: str) -> str:
        if self.chat_template is None:
            raise ValueError("Cannot use roles without a chat template")
        return self.chat_template.get_role_end(role)

    def role_start(self, state: EngineState, node: RoleStart, **kwargs) -> Iterator[OutputAttr]:
        state.active_role = node.role
        # TODO: mark these as special tokens..?
        yield from self.run(state, LiteralNode(value=self.get_role_start(node.role)), **kwargs)

    def role_end(self, state: EngineState, node: RoleEnd, **kwargs) -> Iterator[OutputAttr]:
        state.active_role = None
        # TODO: mark these as special tokens..?
        yield from self.run(state, LiteralNode(value=self.get_role_end(node.role)), **kwargs)

    def text(self, state: EngineState, node: LiteralNode, **kwargs) -> Iterator[OutputAttr]:
        state.prompt += node.value
        yield TextOutput(value=node.value, is_input=True)

    def grammar(self, state: EngineState, node: GrammarNode, **kwargs) -> Iterator[OutputAttr]:
        message_id = self._message_id
        self._message_id += 1

        thread_queue = ThreadQueue()
        self._outstanding[message_id] = thread_queue

        self._work_queue.put((message_id, "grammar", state, node.ll_grammar()))

        exception, chunks = thread_queue.get()
        if exception is not None:
            raise exception

        for chunk in chunks:
            if isinstance(chunk, TextOutput):
                state.prompt += chunk.value
                yield chunk
            elif isinstance(chunk, tuple):
                yield state.apply_capture(*chunk)
            else:
                raise Exception("Unexpected chunk type")

class Llama3VisionClient(EngineClient):
    def image_blob(self, state: EngineState, node: ImageBlob, **kwargs) -> Iterator[OutputAttr]:
        try:
            import PIL.Image
        except ImportError:
            raise Exception(
                "Please install the Pillow package `pip install Pillow` in order to use images with Llama3!"
            )

        image_bytes = b64decode(node.data)
        pil_image = PIL.Image.open(BytesIO(image_bytes))
        state.images.append(pil_image)
        state.prompt += "<|image|>"

        yield ImageOutput(value=node.data, input=True)


class Phi3VisionClient(EngineClient):
    def image_blob(self, state: EngineState, node: ImageBlob, **kwargs) -> Iterator[OutputAttr]:
        try:
            import PIL.Image
        except ImportError:
            raise Exception(
                "Please install the Pillow package `pip install Pillow` in order to use images with Llama3!"
            )

        image_bytes = b64decode(node.data)
        pil_image = PIL.Image.open(BytesIO(image_bytes))

        if pil_image in state.images:
            ix = state.images.index(pil_image) + 1
        else:
            state.images.append(pil_image)
            ix = len(state.images)
        state.prompt += f"<|image_{ix}|>"

        yield ImageOutput(value=node.data, input=True)


def partial_decode(data: bytes) -> tuple[str, bytes]:
    try:
        return (data.decode("utf-8"), b"")
    except UnicodeDecodeError as e:
        valid_part = data[: e.start].decode("utf-8")
        delayed_part = data[e.start :]
    return (valid_part, delayed_part)


def local_worker(outstanding: dict, response_queue: ProcessQueue):
    while True:
        event_id, *response_args = response_queue.get()
        if event_id == 0:
            break
        thread_queue = outstanding[event_id]
        del outstanding[event_id]
        thread_queue.put(response_args)

def remote_worker(work_queue: ProcessQueue, response_queue: ProcessQueue):
    engine = None
    while True:
        event_id, work_type, *work_args = work_queue.get()
        if work_type == "init":
            try:
                engine_type, args, kwargs = work_args
                engine = engine_type(*args, **kwargs)
                chat_template = engine.get_chat_template()

                response_queue.put((event_id, None, chat_template))
            except Exception as e:
                response_queue.put((event_id, e))
        elif work_type == "grammar":
            try:
                state, grammar = work_args

                engine_gen = engine.execute_grammar(
                    state,
                    grammar,
                    ensure_bos_token=True,
                    echo=False,
                )

                chunks = []

                delayed_bytes = b""
                for chunk in engine_gen:
                    new_bytes = chunk.new_bytes
                    new_text, delayed_bytes = partial_decode(new_bytes)

                    # Update the state
                    chunks.append(TextOutput(value=new_text, token_count=chunk.new_token_count, is_generated=True))

                    # TODO -- rewrite engine internals to make sure chunk.{generated,fast_forwarded}_tokens aren't empty...
                    # # TODO: GenTokenExtra
                    # for token in chunk.generated_tokens:
                    #     yield TextOutput(
                    #         value=token.text,  # TODO: this should really be the token bytes
                    #         is_generated=True,
                    #         token_count=1,
                    #         prob=token.prob,
                    #         tokens=[token],  # TODO: drop this
                    #     )
                    # for token in chunk.force_forwarded_tokens:
                    #     yield TextOutput(
                    #         value=token.text,  # TODO: this should really be the token bytes
                    #         is_generated=False,
                    #         token_count=1,
                    #         prob=token.prob,
                    #         tokens=[token],  # TODO: drop this
                    #     )

                    # # TODO: yield some kind of backtrack signal?

                    for name in chunk.capture_groups.keys():
                        values = chunk.capture_groups[name]
                        log_probs = chunk.capture_group_log_probs[name]
                        if isinstance(values, list):
                            assert isinstance(log_probs, list)
                            assert len(values) == len(log_probs)
                            for value, log_prob in zip(values, log_probs):
                                chunks.append((name, value.decode("utf-8"), log_prob, True))
                        else:
                            assert isinstance(log_probs, float)
                            chunks.append((name, values.decode("utf-8"), log_probs, False))

                if delayed_bytes:
                    raise RuntimeError("Shouldn't have any delayed bytes left...")

                response_queue.put((event_id, None, chunks))
            except Exception as e:
                response_queue.put((event_id, e))

        elif work_type == "close":
            break
        else:
            raise Exception("Unknown message type.")

