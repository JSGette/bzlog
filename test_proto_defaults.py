"""Proto3 scalar defaults differ from an absent containing message."""
import unittest
from bzlog import Container


class ProtoDefaultsTest(unittest.TestCase):
    def test_exit_message_with_omitted_zero_is_success(self):
        for code in ({}, {'name': 'SUCCESS'}):
            c = Container()
            c.handle({'finished': {'exitCode': code}})
            self.assertEqual(c.invocation['exit']['code'], 0)
            self.assertTrue(c.invocation['overall_success'])

    def test_missing_exit_message_does_not_invent_success(self):
        c = Container()
        c.handle({'finished': {}})
        self.assertIsNone(c.invocation['exit']['code'])
        self.assertNotIn('overall_success', c.invocation)

    def test_exit_code_is_authoritative_over_deprecated_boolean(self):
        c = Container()
        c.handle({'finished': {'exitCode': {'code': 3}, 'overallSuccess': True}})
        self.assertFalse(c.invocation['overall_success'])

    def test_missing_target_success_is_failure(self):
        c = Container()
        c.handle({'completed': {}})
        c.handle({'completed': {'success': True}})
        self.assertEqual(c.counts['targets_failed'], 1)


if __name__ == '__main__':
    unittest.main()
