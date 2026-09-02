

def is_remote(locations, title=None, ats_remote_flag=None):
    """
    Decide whether a job is remote.
    """
    if ats_remote_flag is not None:
        return bool(ats_remote_flag)

    locations = (locations or "")
    title = (title or "")

    return bool(
        "remote" in locations.lower()
        or "remote" in title.lower()
    )
