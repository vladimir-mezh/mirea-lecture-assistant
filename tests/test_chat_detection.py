from mirea_lecture_assistant.chat_detection import (
    attendance_like_messages,
    chat_baseline,
    classmates_report_attendance_issue,
)

GROUP = "ИКБО-01-24"
OWN = "Иванов Иван ИКБО-01-24"


def test_detects_a_roll_call_of_three_classmates():
    page = """
    Чат
    10:41 Петров Пётр ИКБО-01-24
    QR не работает
    Сидорова Анна ИКБО-01-24
    Кузнецов Илья икбо-01-24
    """
    assert classmates_report_attendance_issue(page, GROUP, OWN)


def test_groups_do_not_have_to_match_ours():
    """A stream lecture: classmates from other groups answer the same roll call."""
    page = """
    Петров Пётр БСБО-11-23
    Смирнова Анна КВБО-07-25
    Орлов Олег ИКБО-02-24
    """
    assert classmates_report_attendance_issue(page, GROUP, OWN)


def test_ignores_own_duplicate_and_unrelated_chat():
    page = f"""
    {OWN}
    {OWN}
    Петров Пётр ИКБО-02-24
    Иванов Иван, отметьте меня пожалуйста
    """
    assert attendance_like_messages(page, GROUP, OWN) == ("Петров Пётр ИКБО-02-24",)
    assert not classmates_report_attendance_issue(page, GROUP, OWN)


def test_two_messages_are_not_a_roll_call():
    page = "Петров Пётр ИКБО-01-24\nСидорова Анна ИКБО-01-24"
    assert not classmates_report_attendance_issue(page, GROUP, OWN)


def test_lowercase_chatter_mentioning_a_group_is_not_a_name():
    page = "кто из ИКБО-01-24\nвсем привет ИКБО-01-24\nа где ИКБО-01-24"
    assert attendance_like_messages(page, GROUP, OWN) == ()


def test_names_already_on_screen_when_joining_do_not_count():
    """Display names in the chat history used to trigger the message at every lecture."""
    history = "Петров Пётр ИКБО-01-24\nСидорова Анна ИКБО-01-24\nОрлов Олег ИКБО-01-24"
    baseline = chat_baseline(history, GROUP, OWN)

    assert not classmates_report_attendance_issue(history, GROUP, OWN, baseline=baseline)
    later = (
        history + "\nКузнецов Илья ИКБО-01-24\nЛебедева Ольга ИКБО-01-24\nЗайцев Иван ИКБО-01-24"
    )
    assert classmates_report_attendance_issue(later, GROUP, OWN, baseline=baseline)
