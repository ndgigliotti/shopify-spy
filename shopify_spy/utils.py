def uri_params(params, spider):
    """Returns URI parameters as dict."""
    return {**params, "spider_name": spider.name}


def as_bool(value):
    """Interpret a string representation of a truth value as True or False.

    True values are 'y', 'yes', 't', 'true', 'on', and '1'; false values
    are 'n', 'no', 'f', 'false', 'off', '0', 'none', 'nan', 'na', and 'null'.
    Case insensitive. Raises ValueError if `value` is anything else. Expects
    a string or something castable as a string.
    
    
    Based on distutils.util.strtobool.
    """
    value = str(value).lower()
    if value in {"y", "yes", "t", "true", "on", "1"}:
        new_value = True
    elif value in {"n", "no", "f", "false", "off", "0", "none", "nan", "na", "null"}:
        new_value = False
    else:
        raise ValueError(f"Could not interpret '{value}' as bool.")
    return new_value
