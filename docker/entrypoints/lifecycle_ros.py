"""Bounded lifecycle calls through a node whose executor is already spinning.

Reuse the supervisor's ROS session instead of starting/discovering/closing a
new ros2 CLI session for every probe and shutdown transition. Calls originate
outside the executor thread; it must keep spinning until driver.stop finishes.
"""

import threading
import time


class LifecycleClient:
    def __init__(self, node, driver_node):
        from lifecycle_msgs.msg import Transition
        from lifecycle_msgs.srv import ChangeState, GetState

        name = "/" + driver_node.strip("/")
        self.get_state = node.create_client(GetState, name + "/get_state")
        self.change_state = node.create_client(ChangeState, name + "/change_state")
        self.get_request = GetState.Request
        self.change_request = ChangeState.Request
        self.transitions = {"deactivate": Transition.TRANSITION_DEACTIVATE,
                            "cleanup": Transition.TRANSITION_CLEANUP}

    @staticmethod
    def call(client, request, timeout):
        deadline = time.monotonic() + timeout
        if not client.wait_for_service(timeout_sec=timeout):
            raise TimeoutError(f"lifecycle service unavailable: {client.srv_name}")
        future = client.call_async(request)
        done = threading.Event()
        future.add_done_callback(lambda _: done.set())
        try:
            if not done.wait(max(0, deadline - time.monotonic())):
                raise TimeoutError(f"lifecycle response timed out: {client.srv_name}")
            result = future.result()
            if result is None:
                raise RuntimeError(f"lifecycle response missing: {client.srv_name}")
            return result
        finally:
            # A late response must not accumulate futures after failed probes.
            client.remove_pending_request(future)

    def state(self, timeout=5):
        return self.call(self.get_state, self.get_request(), timeout).current_state.label

    def transition(self, name, timeout=5):
        request = self.change_request()
        request.transition.id = self.transitions[name]
        if not self.call(self.change_state, request, timeout).success:
            raise RuntimeError(f"driver rejected lifecycle {name}")
