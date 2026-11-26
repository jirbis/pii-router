import json

from pii_router import anthropic_api, openai_api


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def parse(raw: bytes):
    out = []
    for block in raw.decode().split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data:"):
                out.append(json.loads(line[5:]))
    return out


# ------------------------------------------------------------- OpenAI Responses

def test_responses_request(flt):
    payload = {"instructions": "be brief", "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "I am Jane Doe, jane@example.com"}]},
        {"type": "function_call", "name": "mail", "arguments": json.dumps({"to": "jane@example.com"})},
        {"type": "function_call_output", "output": "sent to jane@example.com"},
        {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "data:..."}]},
    ]}
    stats = openai_api.redact_request(flt, payload)
    body = json.dumps(payload)
    assert "jane@example.com" not in body and "Jane Doe" not in body
    assert json.loads(payload["input"][1]["arguments"])["to"].startswith("[PII:EMAIL_")
    assert stats["_unfiltered"] == 1


def test_responses_sse_split_tag_and_arguments(flt):
    tag = flt._tag("PERSON", 'Jane "J" Doe')
    half = len(tag) // 2
    raw = sse([
        {"type": "response.output_text.delta", "item_id": "m", "output_index": 0, "content_index": 0, "delta": "Hi " + tag[:half]},
        {"type": "response.output_text.delta", "item_id": "m", "output_index": 0, "content_index": 0, "delta": tag[half:] + "!"},
        {"type": "response.function_call_arguments.delta", "item_id": "f", "output_index": 1, "delta": '{"to": "' + tag + '"}'},
        {"type": "response.completed", "response": {"output": [{"type": "function_call", "arguments": '{"to": "' + tag + '"}'}]}},
    ])
    r = openai_api.SseRestorer(flt)
    events = parse(r.feed(raw[:37]) + r.feed(raw[37:]) + r.close())
    text = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
    assert text == 'Hi Jane "J" Doe!'
    args = "".join(e["delta"] for e in events if e["type"] == "response.function_call_arguments.delta")
    assert json.loads(args)["to"] == 'Jane "J" Doe'
    done = events[-1]["response"]["output"][0]["arguments"]
    assert json.loads(done)["to"] == 'Jane "J" Doe'


# ------------------------------------------------------------- Anthropic Messages

def test_messages_request_keeps_thinking(flt):
    payload = {"system": [{"type": "text", "text": "You help jane@example.com"}], "messages": [
        {"role": "user", "content": "I'm Jane Doe"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "user Jane Doe", "signature": "sig"},
            {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "mail jane@example.com"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": "ok jane@example.com"}]},
            {"type": "image", "source": {"type": "base64", "data": "..."}}]},
    ]}
    stats = anthropic_api.redact_request(flt, payload)
    msgs = payload["messages"]
    assert msgs[1]["content"][0]["thinking"] == "user Jane Doe"        # signed: unchanged
    rest = json.dumps([payload["system"], msgs[0], msgs[1]["content"][1], msgs[2]])
    assert "jane@example.com" not in rest and "Jane Doe" not in rest
    assert stats["_unfiltered"] == 1


def test_messages_sse(flt):
    tag = flt._tag("PERSON", 'Jane "J" Doe')
    raw = sse([
        {"type": "message_start", "message": {"content": []}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": tag}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hello " + tag[:5]}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": tag[5:]}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"who": "' + tag[:9]}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": tag[9:] + '"}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_stop"},
    ])
    r = anthropic_api.SseRestorer(flt)
    out = b"".join(r.feed(raw[i:i + 50]) for i in range(0, len(raw), 50)) + r.close()
    events = parse(out)
    deltas = [e for e in events if e["type"] == "content_block_delta"]
    assert [d["delta"]["thinking"] for d in deltas if d["index"] == 0] == [tag]
    assert "".join(d["delta"]["text"] for d in deltas if d["index"] == 1) == 'Hello Jane "J" Doe'
    js = "".join(d["delta"]["partial_json"] for d in deltas if d["index"] == 2)
    assert json.loads(js) == {"who": 'Jane "J" Doe'}
    # Order: every delta of a block comes before its stop.
    kinds = [(e["type"], e.get("index")) for e in events]
    assert kinds.index(("content_block_stop", 1)) > max(i for i, k in enumerate(kinds) if k == ("content_block_delta", 1))
