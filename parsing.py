import os
import hashlib


def sha1(string):
    return hashlib.sha1(string.encode("utf-8")).hexdigest()


def write_test_page(response, dst):
    os.makedirs(dst, exist_ok=True)
    path = os.path.join(dst, sha1(response.request.url) + ".html")
    with open(path, "wb") as f:
        f.write(response.body)


def drop_dups(seq):
    seen = set()
    add = seen.add
    return [x for x in seq if not (x in seen or add(x))]
