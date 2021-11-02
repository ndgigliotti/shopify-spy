def uri_params(params, spider):
    """Returns URI parameters as dict."""
    return {**params, "spider_name": spider.name}
