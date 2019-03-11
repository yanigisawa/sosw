__all__ = ['TaskManager']
__author__ = "Nikolay Grishchenko"
__version__ = "1.0"

import boto3
import json
import logging
import math
import os
import time

from collections import defaultdict
from typing import Dict, List, Optional

from sosw.components.benchmark import benchmark
from sosw.labourer import Labourer
from sosw.app import Processor


logger = logging.getLogger()
logger.setLevel(logging.INFO)


class TaskManager(Processor):
    DEFAULT_CONFIG = {
        'init_clients':                ['DynamoDb', 'lambda', 'Ecology'],
        'dynamo_db_config':            {
            'table_name':       'sosw_tasks',
            'index_greenfield': 'sosw_tasks_greenfield',
            'row_mapper':       {
                'task_id':      'S',
                'labourer_id':  'S',
                'created_at':   'N',
                'completed_at': 'N',
                'completed':    'N',
                'greenfield':   'N',
                'attempts':     'N',
                'closed':       'N',
            },
            'required_fields':  ['task_id', 'labourer_id', 'created_at', 'greenfield'],

            # You can overwrite field names to match your DB schema. But the types should be the same.
            # By default takes the key itself.
            'field_names':      {
                'task_id': 'task_id',
                'closed_at': 'closed'
            }
        },
        'sosw_closed_tasks_table': 'sosw_closed_tasks',
        'greenfield_invocation_delta': 31557600,  # 1 year.
        'labourers': {
            # 'some_function': {
            #     'arn': 'arn:aws:lambda:us-west-2:0000000000:function:some_function',
            #     'max_simultaneous_invocations': 10,
            # }
        },
        'max_attempts': 3,
        'min_health_for_retry': 1
    }

    def calculate_greenfield_for_retry_task_with_delay(self, task: Dict, delay: int):
        """
        Returned value is supposed to be some greenfield value assuming that we want the task to be invoked after
        the time following the formula: `Labourer[maximum_runtime] * delay`


        :param task:
        :param delay: incremental coefficient for delay
        :return:
        """

        labourer = None
        # labourer = self.get_labourer() OR receive from call arguments

        queue_length = self.get_length_of_queue_for_labourer(labourer=labourer)

        wanted_delay = labourer.max_duration * delay

        beginning_of_queue = self.get_oldest_greenfield_for_labourer()


        # If queue is smaller than wanted delay we just put the new greenfield in the future.
        if wanted_delay > queue_length * labourer.average_duration:
            return time.time() + wanted_delay

        # Find the position in queue
        else:
            wanted_position = math.ceil(wanted_delay / labourer.average_duration)

            target = self.get_queued_task_for_labourer_in_position(wanted_position)
            return target.greenfield - 1


    def get_queued_task_for_labourer_in_position(self):
        """ implement me """


    def get_oldest_greenfield_for_labourer(self):
        """ Return value of oldest greenfield in queue. """


    def get_length_of_queue_for_labourer(self, labourer: Labourer) -> int:
        """
        Approximate count of tasks still in queue for `labourer`.
        Tasks with greenfield <= now()

        :param labourer:
        :return:
        """






    def register_labourers(self) -> List[Labourer]:
        """ Sets timestamps, health status and other custom attributes on Labourer objects passed for registration. """

        # This must be something ordered, because these methods depend on one another.
        custom_attributes = (
            ('start', lambda x: int(time.time())),
            ('invoked', lambda x: x.get_attr('start') + self.config['greenfield_invocation_delta']),
            ('expired', lambda x: x.get_attr('invoked') - (x.duration + x.cooldown)),
            ('health', lambda x: self.ecology_client.get_labourer_status(x)),
            ('max_attempts', lambda x: self.config.get(f'max_attempts_{x.id}') or self.config['max_attempts']),
            ('min_health_for_retry', lambda x: self.config.get(f'min_health_for_retry_{x.id}') or self.config['min_health_for_retry']),
            ('average_duration', lambda x: self.ecology_client.get_labourer_average_duration_(x)),
        )

        labourers = self.get_labourers()

        result = []
        for labourer in labourers:
            for k, method in [x for x in custom_attributes]:
                labourer.set_custom_attribute(k, method(labourer))
                print(f"SET for {labourer}: {k} = {method(labourer)}")
            result.append(labourer)

        return result


    def get_db_field_name(self, key: str) -> str:
        """ Could be useful if you overwrite field names with your own ones (e.g. for tests). """
        return self.config['dynamo_db_config']['field_names'].get(key, key)


    def create_task(self, **kwargs):
        raise NotImplementedError


    def invoke_task(self, labourer: Labourer, task_id: Optional[str] = None, task: Optional[Dict] = None):
        """ Invoke the Lambda Function execution for `task` """

        if not any([task, task_id]) or all([task, task_id]):
            raise AttributeError(f"You must provide any of `task` or `task_id`.")

        task = self.get_task_by_id(task_id=task_id)

        try:
            self.mark_task_invoked(labourer, task)
        except Exception as err:
            if err.__class__.__name__ == 'ConditionalCheckFailedException':
                logger.warning(f"Update failed due to already running task {task}. "
                               f"Probably concurrent Orchestrator already invoked.")
                self.stats['concurrent_task_invocations_skipped'] += 1
                return
            else:
                logger.exception(err)
                raise RuntimeError(err)


        lambda_response = self.lambda_client.invoke(
                FunctionName=labourer.arn,
                InvocationType='Event',
                Payload=task.get('payload')
        )
        logger.debug(lambda_response)


    def mark_task_invoked(self, labourer: Labourer, task: Dict, check_running: Optional[bool] = True):
        """
        Update the greenfield with the latest invocation timestamp + invocation_delta

        By default updates with a conditional expression that fails in case the current greenfield is already in
        `invoked` state. If this check fails the function raises RuntimeError that should be handled
        by the Orchestrator. This is very important to help duplicate invocations of the Worker by simultaneously
        running Orchestrators.

        :param labourer:        Labourer for the task
        :param task:            Task dictionary
        :param check_running:   If True (default) updates with conditional expression.
        :raises RuntimeError
        """

        tf = self.get_db_field_name('task_id')  # Main key field
        lf = self.get_db_field_name('labourer_id')  # Range key field
        gf = self.get_db_field_name('greenfield')
        af = self.get_db_field_name('attempts')

        assert labourer.id == task[lf], f"Task doesn't belong to the Labourer {labourer}: {task}"

        self.dynamo_db_client.update(
                {tf: task[tf], lf: labourer.id},
                attributes_to_update={gf: int(time.time()) + self.config['greenfield_invocation_delta']},
                attributes_to_increment={af: 1},
                condition_expression=f"{gf} < {labourer.get_attr('start')}"
        )


    def close_task(self, task_id: str, labourer_id: str, completed: bool):
        _ = self.get_db_field_name

        completed = int(completed)

        self.dynamo_db_client.update(
                {_('task_id'): task_id, _('labourer_id'): labourer_id},
                attributes_to_update={_('closed_at'): int(time.time()), _('completed'): completed},
        )


    def archive_task(self, task_id: str):
        _ = self.get_db_field_name

        # Get task
        task = self.get_task_by_id(task_id)

        # Update labourer_id_task_status field.
        is_completed = 1 if task.get(_('completed_at')) else 0
        labourer_id = task.get(_('labourer_id'))
        task[_('labourer_id_task_status')] = f"{labourer_id}_{is_completed}"

        # Add it to completed tasks table:
        self.dynamo_db_client.put(task, table_name=self.config.get('sosw_closed_tasks_table'))

        # Delete it from tasks_table
        keys = {
            _('labourer_id'): task[_('labourer_id')],
            _('task_id'): task[_('task_id')],
        }
        self.dynamo_db_client.delete(keys)


    def get_task_by_id(self, task_id: str) -> Dict:
        """ Fetches the full data of the Task. """

        tasks = self.dynamo_db_client.get_by_query({self.get_db_field_name('task_id'): task_id})
        return tasks[0] if tasks else None


    def get_next_for_labourer(self, labourer: Labourer, cnt: int = 1) -> List[str]:
        """
        Fetch the next task(s) from the queue for the Labourer.

        :param labourer:   Labourer to get next tasks for.
        :param cnt:        Optional number of Tasks to fetch.
        """

        # Maximum value to identify the task as available for invocation (either new, or ready for retry).
        max_greenfield = labourer.get_attr('start')

        result = self.dynamo_db_client.get_by_query(
                {
                    self.get_db_field_name('labourer_id'): labourer.id,
                    self.get_db_field_name('greenfield'):  max_greenfield
                },
                table_name=self.config['dynamo_db_config']['table_name'],
                index_name=self.config['dynamo_db_config']['index_greenfield'],
                strict=True,
                max_items=cnt,
                comparisons={
                    self.get_db_field_name('greenfield'): '<'
                })

        logger.info(f"get_next_for_labourer() received: {result} from {self.config['dynamo_db_config']['table_name']} "
                    f"for labourer: {labourer.id} max greenfield: {max_greenfield}")

        return [task[self.get_db_field_name('task_id')] for task in result]


    def calculate_count_of_running_tasks_for_labourer(self, labourer: Labourer) -> int:
        """
        Returns a number of tasks we assume to be still running.
        Theoretically they can be dead with Exception, but not yet expired.
        """

        return len(self.get_running_tasks_for_labourer(labourer=labourer))


    def get_invoked_tasks_for_labourer(self, labourer: Labourer, closed: Optional[bool] = None) -> List[Dict]:
        """
        Return a list of tasks of current Labourer invoked during the current run of the Orchestrator.

        If closed is provided:
        * True - filter closed ones
        * False - filter NOT closed ones
        * None (default) - do not care about `closed` status.
        """

        # lf = self.get_db_field_name('labourer_id')
        # gf = self.get_db_field_name('greenfield')
        _ = self.get_db_field_name

        query_args = {
            'keys':        {
                _('labourer_id'): labourer.id,
                _('greenfield'): labourer.get_attr('invoked')
            },
            'comparisons': {_('greenfield'): '>='},
            'index_name':  self.config['dynamo_db_config']['index_greenfield'],
        }

        if closed is True:
            query_args['filter_expression'] = f"attribute_exists {_('closed_at')}"
        elif closed is False:
            query_args['filter_expression'] = f"attribute_not_exists {_('closed_at')}"
        else:
            logger.debug(f"No filtering by closed status for {query_args}")

        return self.dynamo_db_client.get_by_query(**query_args)


    def get_running_tasks_for_labourer(self, labourer: Labourer) -> List[Dict]:
        """
        Return a list of tasks of Labourer previously invoked, but not yet closed or expired.
        We assume they are still running.
        """

        _ = self.get_db_field_name

        return self.dynamo_db_client.get_by_query(
                keys={
                    _('labourer_id'):                 labourer.id,
                    f"st_between_{_('greenfield')}": labourer.get_attr('expired'),
                    f"en_between_{_('greenfield')}": labourer.get_attr('invoked'),
                },
                index_name=self.config['dynamo_db_config']['index_greenfield'],
                filter_expression=f'attribute_not_exists {_("closed_at")}'
        )


    def get_closed_tasks_for_labourer(self, labourer: Labourer) -> List[Dict]:
        """
        Return a list of tasks of the Labourer marked as closed.
        Scavenger is supposed to archive them all so no special filtering is required here.

        In order to be able to use the already existing `index_greenfield`, we sort tasks only in invoked stages
        (`greenfield > now()`). This number is supposed to be small, so filtering by an un-indexed field will be fast.
        """

        return self.get_invoked_tasks_for_labourer(labourer=labourer, closed=True)


    def get_expired_tasks_for_labourer(self, labourer: Labourer) -> List[Dict]:
        """ Return a list of tasks of Labourer previously invoked, and expired without being closed. """

        _ = self.get_db_field_name

        return self.dynamo_db_client.get_by_query(
                keys={
                    _('labourer_id'):                 labourer.id,
                    f"st_between_{_('greenfield')}": labourer.get_attr('start'),
                    f"en_between_{_('greenfield')}": labourer.get_attr('expired'),
                },
                index_name=self.config['dynamo_db_config']['index_greenfield'],
                filter_expression=f"attribute_not_exists {_('closed_at')}",
        )


    def get_labourers(self) -> List[Labourer]:
        """
        Return configured Labourers.
        Config of the TaskManager expects 'labourers' as a dict 'name_of_lambda': {'some_setting': 'value1'}
        """

        # TODO Should return self.labourers or smth

        return [Labourer(id=name, **settings) for name, settings in self.config['labourers'].items()]
