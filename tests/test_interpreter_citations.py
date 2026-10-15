"""The interpreter fallback keeps only fields whose citations resolve in the recorded traffic."""
from forge.models.trace import Exchange
from forge.stages.extract.interpreter import Citation, InterpretedField, InterpretedOperation, Interpretation, resolve, verify


def gql_exchange() -> Exchange:
    return Exchange(
        id="x0007", step_id="s004", correlated_by="nonce", method="POST", url="http://a/graphql", path="/graphql",
        resource_type="fetch", status=200, request_content_type="application/json",
        request_body='{"operationName":"CreateIssue","variables":{"input":{"title":"Fix rpl-1"}}}',
        response_content_type="application/json",
        response_body='{"data":{"createIssue":{"id":"9","title":"Fix rpl-1","labels":[{"name":"bug"}]}}}',
        started_at=0.0)


def test_resolve_paths():
    data = {"data": {"items": [{"title": "a"}]}}
    assert resolve(data, "data.items[0].title") == "a"


def test_verify_rejects_invented_fields():
    ex = gql_exchange()
    good = InterpretedField(name="title", json_type="string",
                            citation=Citation(exchange_id="x0007", side="response", json_path="data.createIssue.title"))
    invented = InterpretedField(name="priority", json_type="string",
                                citation=Citation(exchange_id="x0007", side="response", json_path="data.createIssue.priority"))
    wrong_exchange = InterpretedField(name="labels", json_type="array",
                                      citation=Citation(exchange_id="x9999", side="response", json_path="data"))
    op = InterpretedOperation(
        id="createIssue", kind="create", entity="issue", method="POST", path="/graphql", discriminator="CreateIssue",
        request_fields=[InterpretedField(name="title", json_type="string", citation=Citation(
            exchange_id="x0007", side="request", json_path="variables.input.title"))],
        response_fields=[good, invented, wrong_exchange],
        evidence=[Citation(exchange_id="x0007", side="request", json_path="operationName")])
    kept, rejected = verify(Interpretation(operations=[op], notes=[]), {"x0007": ex})
    assert rejected == 2
    assert [f.name for f in kept[0].response_fields] == ["title"]
    assert [f.name for f in kept[0].request_fields] == ["title"]
