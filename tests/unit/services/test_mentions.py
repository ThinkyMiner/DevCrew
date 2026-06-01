from app.services.mentions import find_mentions


def test_no_mentions_returns_empty() -> None:
    assert find_mentions("just a plain message with no tags") == []


def test_single_handle() -> None:
    assert find_mentions("hey @architect what do you think") == ["architect"]


def test_handles_lowercased() -> None:
    assert find_mentions("ping @Architect and @ReSearcher") == ["architect", "researcher"]


def test_everyone_recognized() -> None:
    assert find_mentions("@everyone please weigh in") == ["everyone"]


def test_preserves_order_and_keeps_duplicates() -> None:
    # find_mentions returns raw occurrences in order; de-duping is the caller's job.
    assert find_mentions("@a then @b then @a") == ["a", "b", "a"]


def test_handle_charset_matches_models() -> None:
    # handles match [a-z0-9_-]+ per domain Persona validator
    assert find_mentions("@my-bot_2 hi") == ["my-bot_2"]


def test_email_not_treated_as_mention() -> None:
    # an @ embedded inside a word (no leading boundary) is not a mention
    assert find_mentions("mail me at kartik@example.com") == []
