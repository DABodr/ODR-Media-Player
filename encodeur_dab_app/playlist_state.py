def move_item(items, current_idx, source_idx, target_idx):
    if source_idx < 0 or target_idx < 0:
        return current_idx
    if source_idx >= len(items) or target_idx >= len(items):
        return current_idx
    if source_idx == target_idx:
        return current_idx

    items[source_idx], items[target_idx] = items[target_idx], items[source_idx]

    if current_idx == source_idx:
        return target_idx
    if current_idx == target_idx:
        return source_idx
    return current_idx


def remove_item(items, current_idx, index):
    if index < 0 or index >= len(items):
        return current_idx

    del items[index]

    if not items:
        return -1
    if current_idx >= len(items):
        return len(items) - 1
    return current_idx
