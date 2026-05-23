from web import _public_state


def test_public_state_normalizes_keys_and_hides_movers():
    raw_state = {
        "session": "regular",
        "results": [
            {"symbol": "AAPL", "change_pct": 1.2, "rel_volume": 2.0, "price": 150.0},
            {"symbol": "MSFT", "change_pct": -0.8, "rel_volume": 1.1, "price": 300.0},
        ],
        "next_run_in": 120,
    }

    state = _public_state(raw_state)

    assert state["selected_session"] == "regular"
    assert "current_market_session" in state
    assert state["estimated_remaining"] == 120
    assert "top_movers" not in state
    assert "most_active" not in state
    assert "unusual_volume" not in state
    assert state["results"][0]["symbol"] == "AAPL"
