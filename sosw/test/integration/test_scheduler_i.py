import boto3
import os
import random
import subprocess
import unittest

from unittest.mock import MagicMock, patch

from sosw.scheduler import Scheduler
from sosw.labourer import Labourer
from sosw.test.variables import TEST_SCHEDULER_CONFIG


os.environ["STAGE"] = "test"
os.environ["autotest"] = "True"


class Scheduler_IntegrationTestCase(unittest.TestCase):
    TEST_CONFIG = TEST_SCHEDULER_CONFIG


    @classmethod
    def setUpClass(cls):
        cls.TEST_CONFIG['init_clients'] = ['S3', ]

        cls.clean_bucket()


    @staticmethod
    def clean_bucket():
        """ Clean S3 bucket"""

        s3 = boto3.resource('s3')
        bucket = s3.Bucket('autotest-bucket')
        bucket.objects.all().delete()


    def exists_in_s3(self, key):
        try:
            self.s3_client.get_object(Bucket='autotest-bucket', Key=key)
            return True
        except self.s3_client.exceptions.ClientError:
            return False


    def put_file(self, local=None, key=None):
        with open(local or self.scheduler._local_queue_file, 'w') as f:
            for _ in range(10):
                f.write(f"hello Liat {random.randint(0,99)}\n")

        self.s3_client.upload_file(Filename=local or self.scheduler._local_queue_file,
                                   Bucket='autotest-bucket',
                                   Key=key or self.scheduler._remote_queue_file)


    @staticmethod
    def line_count(file):
        return int(subprocess.check_output('wc -l {}'.format(file), shell=True).split()[0])


    def setUp(self):
        self.patcher = patch("sosw.app.get_config")
        self.get_config_patch = self.patcher.start()

        self.custom_config = self.TEST_CONFIG.copy()
        self.scheduler = Scheduler(self.custom_config)

        self.s3_client = boto3.client('s3')


    def tearDown(self):
        self.patcher.stop()
        self.clean_bucket()

        try:
            del (os.environ['AWS_LAMBDA_FUNCTION_NAME'])
        except:
            pass


    def test_true(self):
        self.assertEqual(1, 1)


    def test_get_and_lock_queue_file(self):
        self.put_file()

        # Check old artifacts
        self.assertFalse(self.exists_in_s3(self.scheduler._remote_queue_locked_file))
        self.assertTrue(self.exists_in_s3(self.scheduler._remote_queue_file))

        r = self.scheduler.get_and_lock_queue_file()

        self.assertEqual(r, self.scheduler._local_queue_file)

        self.assertTrue(self.exists_in_s3(self.scheduler._remote_queue_locked_file))
        self.assertFalse(self.exists_in_s3(self.scheduler._remote_queue_file))

        number_of_lines = self.line_count(self.scheduler._local_queue_file)
        # print(f"Number of lines: {number_of_lines}")
        self.assertTrue(number_of_lines, 10)


    def test_upload_and_unlock_queue_file(self):

        # Check old artifacts
        self.assertFalse(self.exists_in_s3(self.scheduler._remote_queue_locked_file))
        self.assertFalse(self.exists_in_s3(self.scheduler._remote_queue_file))

        with open(self.scheduler._local_queue_file, 'w') as f:
            for _ in range(10):
                f.write(f"Hello Demida {random.randint(0,99)}\n")

        self.scheduler.upload_and_unlock_queue_file()

        self.assertFalse(self.exists_in_s3(self.scheduler._remote_queue_locked_file))
        self.assertTrue(self.exists_in_s3(self.scheduler._remote_queue_file))


    def test_upload_and_unlock_queue_file__handles_existing_locked(self):

        self.put_file(key=self.scheduler._remote_queue_locked_file)
        # Check old artifacts
        self.assertFalse(self.exists_in_s3(self.scheduler._remote_queue_locked_file))
        self.assertFalse(self.exists_in_s3(self.scheduler._remote_queue_file))
