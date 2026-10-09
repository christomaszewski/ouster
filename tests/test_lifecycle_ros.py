"""Bounded ROS requests; these adapter tests need no ROS installation."""

from concurrent.futures import Future
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from test_operational_state import ROOT  # adds the entrypoint modules to sys.path
from lifecycle_ros import LifecycleClient


class LifecycleClientTests(unittest.TestCase):
    def client(self, result=None):
        client = Mock(srv_name="/ouster/os_driver/get_state")
        client.wait_for_service.return_value = True
        future = Future()
        if result is not None:
            future.set_result(result)
        client.call_async.return_value = future
        return client, future

    def test_response_from_executor_is_returned(self):
        reply = object()
        client, future = self.client(reply)
        self.assertIs(LifecycleClient.call(client, object(), 1), reply)
        client.remove_pending_request.assert_called_once_with(future)

    def test_missing_service_is_bounded_and_sends_no_request(self):
        client, _ = self.client()
        client.wait_for_service.return_value = False
        with self.assertRaisesRegex(TimeoutError, "unavailable"):
            LifecycleClient.call(client, object(), 0.02)
        client.wait_for_service.assert_called_once_with(timeout_sec=0.02)
        client.call_async.assert_not_called()

    def test_lost_response_removes_pending_request(self):
        client, future = self.client()
        with self.assertRaisesRegex(TimeoutError, "response timed out"):
            LifecycleClient.call(client, object(), 0.02)
        client.remove_pending_request.assert_called_once_with(future)

    def test_future_error_is_propagated_and_removed(self):
        client, future = self.client()
        future.set_exception(RuntimeError("ROS context closed"))
        with self.assertRaisesRegex(RuntimeError, "context closed"):
            LifecycleClient.call(client, object(), 1)
        client.remove_pending_request.assert_called_once_with(future)

    def test_rejected_transition_is_not_reported_as_success(self):
        adapter = LifecycleClient.__new__(LifecycleClient)
        adapter.transitions = {"cleanup": 2}
        adapter.change_request = lambda: SimpleNamespace(transition=SimpleNamespace(id=0))
        adapter.change_state, _ = self.client(SimpleNamespace(success=False))
        with self.assertRaisesRegex(RuntimeError, "rejected lifecycle cleanup"):
            adapter.transition("cleanup")
        self.assertEqual(adapter.change_state.call_async.call_args.args[0].transition.id, 2)
