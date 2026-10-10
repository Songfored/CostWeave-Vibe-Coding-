import json
from unittest.mock import patch
from test_automated_experiments import AutomationTests
from costweave.automated_experiments import FixtureTransport
from costweave.adapters import HTTPResponse


class OptionalModelTests(AutomationTests):
    def test_default_omits_model_without_discovery_and_deduplicates(self):
        payloads=[]
        class Capture(FixtureTransport):
            def post(self, **kwargs):
                payloads.append(json.loads(kwargs['body']))
                return super().post(**kwargs)
        self.auto.transport_factory=lambda task:Capture(task['expected'])
        with patch.object(self.lab, 'discover_models', side_effect=AssertionError('discovery must be optional')):
            first=self.connect(model='',provider_default=True)
            second=self.connect(model='',provider_default=True)
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(len(payloads),8)
        self.assertTrue(all('model' not in p for p in payloads))
        self.assertEqual(self.auto.rounds[first['id']]['status'],'completed')
        third=self.connect(id=first['connection_id'],model='explicit-alias',provider_default=False)
        self.assertNotEqual(first['id'],third['id'])
        self.assertTrue(all(p['model']=='explicit-alias' for p in payloads[8:]))

    def test_required_model_rejection_stops_after_one_attempt(self):
        payloads=[]
        class Reject(FixtureTransport):
            def post(self,**kwargs):
                payloads.append(json.loads(kwargs['body']))
                return HTTPResponse(400,b'{"error":{"message":"model required"}}',{},1)
        self.auto.transport_factory=lambda task:Reject(task['expected'])
        result=self.connect(model='',provider_default=True)
        row=self.auto.rounds[result['id']]
        self.assertEqual(row['status'],'failed')
        self.assertEqual(len(payloads),1)
        self.assertIn('model',row['message'])

    def test_default_profiles_remove_field(self):
        from costweave.experiment_lab import lab_adapter_type
        from types import SimpleNamespace
        for profile in ('compatible','openai','deepseek','responses'):
            cls=lab_adapter_type(profile,True)
            with patch.object(cls.__bases__[0], 'build_payload', return_value={'model':'sentinel','input':'keep'}):
                self.assertEqual(cls.build_payload(object.__new__(cls),SimpleNamespace()),{'input':'keep'})
