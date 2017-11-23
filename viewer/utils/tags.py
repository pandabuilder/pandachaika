scope_priorities = ["language", "artist", "group", "parody", "character", "female", "male", "misc"]


def sort_tags(tag_list):

    prioritized_tag_list = list()

    for scope in scope_priorities:
        matched_tags = sorted([x for x in tag_list if x.scope == scope], key=str)
        if matched_tags:
            prioritized_tag_list.append((scope, matched_tags))

    remaining_tags = sorted([x for x in tag_list if x.scope not in scope_priorities], key=str)
    if remaining_tags:
        prioritized_tag_list.append((None, remaining_tags))

    return prioritized_tag_list


def sort_tags_str(tag_list):

    prioritized_tag_list = list()

    for scope in scope_priorities:
        matched_tags = sorted([str(x) for x in tag_list if x.scope == scope], key=str)
        if matched_tags:
            prioritized_tag_list.extend(matched_tags)

    remaining_tags = sorted([str(x) for x in tag_list if x.scope not in scope_priorities], key=str)
    if remaining_tags:
        prioritized_tag_list.extend(remaining_tags)

    return prioritized_tag_list
