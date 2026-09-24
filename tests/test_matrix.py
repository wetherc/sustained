import io
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import matrix  # noqa: E402

TARGET = next(name for name in matrix.SERVERS if matrix.needs_container(name))


class ContainerCleanupTests(unittest.TestCase):
    """main() removes the containers once it has asked compose to start them."""

    def _main(self, start, *argv):
        with ExitStack() as stack:

            def patch(name, **kw):
                return stack.enter_context(mock.patch.object(matrix, name, **kw))

            patch("docker_ready", return_value=True)
            patch("missing_reason", return_value=None)
            patch("start", **start)
            patch("run_server", return_value=matrix.Result(TARGET, "passed", ""))
            patch("report", return_value=0)
            stop = patch("stop")
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            try:
                matrix.main([TARGET, *argv])
            except KeyboardInterrupt:
                pass
        return stop

    def test_a_clean_start_is_removed(self):
        stop = self._main({"return_value": True})
        stop.assert_called_once_with(matrix.services_for([TARGET]))

    def test_an_unhealthy_start_is_removed(self):
        stop = self._main({"return_value": False})
        stop.assert_called_once_with(matrix.services_for([TARGET]))

    def test_an_interrupted_start_is_removed(self):
        stop = self._main({"side_effect": KeyboardInterrupt})
        stop.assert_called_once_with(matrix.services_for([TARGET]))

    def test_keep_leaves_the_containers(self):
        stop = self._main({"return_value": False}, "--keep")
        stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
