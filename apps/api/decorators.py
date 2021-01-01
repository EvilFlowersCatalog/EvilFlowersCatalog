from functools import wraps


def require_apikey(view_func):
    """
    Require valid signature for specified endpoint
    :param view_func:
    :return:
    """
    def wrapped_view(*args, **kwargs):
        return view_func(*args, **kwargs)
    wrapped_view.require_apikey = True
    return wraps(view_func)(wrapped_view)
