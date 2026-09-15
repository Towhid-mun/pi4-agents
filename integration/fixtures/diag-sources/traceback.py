def inner(x):
    return 10 / x


def outer():
    return inner(0)


outer()
