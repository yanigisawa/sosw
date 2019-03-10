import os
import unittest
from copy import deepcopy

from unittest.mock import Mock, MagicMock, patch, call

from sosw.scavenger import Scavenger
from sosw.labourer import Labourer
from sosw.test.variables import TEST_SCAVENGER_CONFIG, EXPIRED_TASKS, LABOURERS


os.environ["STAGE"] = "test"
os.environ["autotest"] = "True"


class Scavenger_UnitTestCase(unittest.TestCase):
    TEST_CONFIG = TEST_SCAVENGER_CONFIG


    def setUp(self):
        self.patcher = patch("sosw.app.get_config")
        self.get_config_patch = self.patcher.start()

        self.custom_config = self.TEST_CONFIG.copy()
        self.scavenger = Scavenger(self.custom_config)

        # Mock clients
        self.scavenger.task_client = MagicMock()
        self.scavenger.ecology_client = MagicMock()
        self.scavenger.sns_client = MagicMock()

        self.scavenger.get_db_field_name = MagicMock(side_effect=lambda x: x)
        _ = self.scavenger.get_db_field_name

        self.task = {
            _('task_id'): '123', _('labourer_id'): 'lambda1', _('greenfield'): '3525624', _('payload'): '{"a": 1}',
            _('closed'):  False, _('attempts'): 2
        }


    def tearDown(self):
        self.patcher.stop()

        try:
            del (os.environ['AWS_LAMBDA_FUNCTION_NAME'])
        except:
            pass


    def test_call(self):
        # Mock
        self.scavenger.task_client.register_labourers = Mock(return_value=LABOURERS)
        self.scavenger.handle_expired_tasks_for_labourer = Mock()
        self.scavenger.archive_closed_tasks_for_labourer = Mock()

        # Call
        self.scavenger()

        # Check call
        self.assertEqual(self.scavenger.handle_expired_tasks_for_labourer.call_count, 3)
        self.assertEqual(self.scavenger.archive_closed_tasks_for_labourer.call_count, 3)


    def test_handle_expired_tasks_for_labourer(self):
        labourer = LABOURERS[1]
        expired_tasks_per_lambda = {
            'some_lambda':    [EXPIRED_TASKS[0]],
            'another_lambda': [EXPIRED_TASKS[1], EXPIRED_TASKS[2]]
        }

        self.scavenger.task_client.get_expired_tasks_for_labourer = MagicMock(
                side_effect=lambda l: expired_tasks_per_lambda.get(l.id, []))
        self.scavenger.process_expired_task = Mock()

        # Call
        self.scavenger.handle_expired_tasks_for_labourer(labourer)

        # Check call
        self.scavenger.task_client.get_expired_tasks_for_labourer.assert_called_once_with(labourer)

        self.scavenger.process_expired_task.assert_has_calls(
                [call(EXPIRED_TASKS[1]), call(EXPIRED_TASKS[2])]
        )


    def test_archive_closed_tasks_for_labourer(self):
        labourer = LABOURERS[0]
        closed_tasks_per_lambda = {
            'some_lambda':    [EXPIRED_TASKS[0]],
            'another_lambda': [EXPIRED_TASKS[1]]
        }

        self.scavenger.task_client.get_closed_tasks_for_labourer = MagicMock(
                side_effect=lambda l: closed_tasks_per_lambda.get(l.id, []))
        self.scavenger.task_client.archive_task = Mock()

        self.scavenger.archive_closed_tasks_for_labourer(labourer)

        self.scavenger.task_client.get_closed_tasks_for_labourer.assert_called_once_with(labourer)
        self.scavenger.task_client.archive_task.assert_called_once_with(EXPIRED_TASKS[0]['task_id'])


    def test_process_expired_task__close(self):
        # Mock
        self.scavenger.should_retry_task = Mock(return_value=False)
        self.scavenger.allow_task_to_retry = Mock()
        self.scavenger.task_client.close_dead_task = Mock()

        # Call
        self.scavenger.process_expired_task(self.task)

        # Check mock calls
        self.scavenger.task_client.close_dead_task.assert_called_once_with('123')
        self.scavenger.allow_task_to_retry.assert_not_called()


    def test_process_expired_task__dont_close(self):
        # Mock
        self.scavenger.should_retry_task = Mock(return_value=True)
        self.scavenger.allow_task_to_retry = Mock()
        self.scavenger.task_client.close_dead_task = Mock()

        # Call
        self.scavenger.process_expired_task(self.task)

        # Check mock calls
        self.scavenger.allow_task_to_retry.assert_called_once_with(self.task)
        self.scavenger.task_client.close_dead_task.assert_not_called()


    @unittest.skip("Logic is not yet final")
    def test_should_retry_task(self):
        raise NotImplementedError
