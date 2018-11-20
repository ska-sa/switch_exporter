# Switch exporter

This is a Prometheus exporter that collects counters from a switch and makes
them available for Prometheus to scrape. It has been written specifically for
Mellanox switches running MLNX-OS, but may work on others.

The metric endpoint is `http://HOST:PORT/metrics?target=TARGET`, where TARGET
is the address of the switch. The initial scrape will be slower as it
establishes a connection to the switch, enumerates the ports and gets remote
endpoint information from LLDP.

To configure it in Prometheus, you will most likely want a configuration
something like this, which arranges for `instance` labels to reflect the switch
rather than the exporter service.

```
scrape_configs:
  - job_name: 'switches'
    static_configs:
      - targets:
          - SWITCH1
          - SWITCH2
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: host:port     # endpoint of this exporter
```
