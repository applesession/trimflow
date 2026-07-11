import os
import time

import requests


WAVESPEED_API_BASE = "https://api.wavespeed.ai/api/v3"


def get_wavespeed_api_key():
    return (os.getenv("WAVESPEED_API_KEY") or "").strip()


def _extract_data(payload):
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


def _extract_prediction_id(payload):
    data = _extract_data(payload)
    for candidate in (
        data.get("id"),
        data.get("request_id"),
        payload.get("id") if isinstance(payload, dict) else None,
        payload.get("request_id") if isinstance(payload, dict) else None,
    ):
        if candidate:
            return str(candidate)
    raise RuntimeError(f"WaveSpeed response is missing prediction id: {payload}")


def _extract_status(payload):
    data = _extract_data(payload)
    return str(data.get("status") or payload.get("status") or "").strip().lower()


def _extract_outputs(payload):
    data = _extract_data(payload)
    outputs = data.get("outputs")
    if isinstance(outputs, list):
        return outputs
    if isinstance(payload, dict) and isinstance(payload.get("outputs"), list):
        return payload["outputs"]
    return []


def _extract_output_url(output_item):
    if isinstance(output_item, str):
        return output_item
    if isinstance(output_item, dict):
        for key in ("url", "src", "output", "image"):
            value = output_item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _wavespeed_request(method, path, *, json_payload=None, timeout=60):
    api_key = get_wavespeed_api_key()
    if not api_key:
        raise RuntimeError("WAVESPEED_API_KEY is not configured")

    response = requests.request(
        method,
        f"{WAVESPEED_API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=json_payload,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def submit_edit_prediction(model, payload):
    return _wavespeed_request("POST", f"/{model}", json_payload=payload, timeout=60)


def get_prediction_result(prediction_id):
    return _wavespeed_request("GET", f"/predictions/{prediction_id}/result", timeout=60)


def run_edit_prediction(model, payload, *, timeout_seconds=180, poll_interval_seconds=3):
    submit_payload = dict(payload or {})
    submit_payload.setdefault("enable_sync_mode", False)
    submit_payload.setdefault("enable_base64_output", False)
    submit_response = submit_edit_prediction(model, submit_payload)
    prediction_id = _extract_prediction_id(submit_response)

    deadline = time.monotonic() + max(int(timeout_seconds), 1)
    last_payload = submit_response
    while time.monotonic() < deadline:
        result_payload = get_prediction_result(prediction_id)
        last_payload = result_payload
        status = _extract_status(result_payload)
        if status == "completed":
            outputs = _extract_outputs(result_payload)
            if not outputs:
                raise RuntimeError(f"WaveSpeed prediction completed without outputs: {result_payload}")
            output_url = _extract_output_url(outputs[0])
            if not output_url:
                raise RuntimeError(f"WaveSpeed prediction output URL is missing: {result_payload}")
            return {
                "prediction_id": prediction_id,
                "status": status,
                "output_url": output_url,
                "outputs": outputs,
                "raw": result_payload,
            }
        if status in {"failed", "canceled", "cancelled"}:
            raise RuntimeError(f"WaveSpeed prediction {prediction_id} failed: {result_payload}")
        time.sleep(max(float(poll_interval_seconds), 1.0))

    raise RuntimeError(f"WaveSpeed prediction {prediction_id} timed out: {last_payload}")
