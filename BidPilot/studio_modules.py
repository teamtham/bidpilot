"""Studio features shared by the HTTP service, schema bootstrapping and MCP."""


def modules():
    import change_chat
    import commerce
    import conversation
    import estimating
    import portfolio
    import delivery
    import studio_assistant
    return conversation, estimating, commerce, portfolio, delivery, studio_assistant, change_chat
