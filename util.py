import os
import hashlib


def sha1(string):
    """Returns sha1 hash of string."""
    return hashlib.sha1(string.encode("utf-8")).hexdigest()


def write_test_page(response, dst):
    """Saves response as an html file in directory dst."""
    os.makedirs(dst, exist_ok=True)
    path = os.path.join(dst, sha1(response.request.url) + ".html")
    with open(path, "wb") as f:
        f.write(response.body)


def drop_dups(seq):
    """Returns list without duplicates in original order."""
    seen = set()
    add = seen.add
    return [x for x in seq if not (x in seen or add(x))]
