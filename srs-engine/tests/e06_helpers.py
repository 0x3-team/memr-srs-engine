"""E06 RPC fixture retains unsolicited progress beside matching responses."""
import json

from memr_srs_engine.validate import validate_value
from tests.test_decks import RpcCase as DeckRpcCase


class RpcCase(DeckRpcCase):
    def frame(self, method, params=None):
        self.sequence += 1
        offset = self.output.tell()
        request = {"id": self.sequence, "method": method, "params": {} if params is None else params}
        expected_id = self.sequence
        try:
            json.dumps(request, allow_nan=False)
        except ValueError:
            # Deliberately malformed JSON is rejected before its id is trusted.
            expected_id = None
        self.engine.dispatch(json.dumps(request).encode())
        frames = [json.loads(line) for line in self.output.getvalue()[offset:].splitlines()]
        responses = []
        for frame in frames:
            if frame.get("event") == "progress":
                self.assertEqual(validate_value(self.engine.registry.catalogue["progress.get"]["result"], frame["progress"]), [])
                self.assertEqual(frame["operationId"], frame["progress"]["operationId"])
            else:
                self.assertEqual(frame["id"], expected_id)
                if expected_id is None:
                    self.assertEqual(frame["error"]["code"], "invalid_params")
                responses.append(frame)
        self.assertEqual(len(responses), 1, frames)
        return responses[0]
