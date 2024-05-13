COUNTERS = {
    'packets', 'multicast packets', 'unicast packets', 'broadcast packets',
    'bytes', 'error packets', 'discard packets'
}


def name_to_metric(name: str) -> str:
    return 'switch_port_' + name.replace(' ', '_') + '_total'
