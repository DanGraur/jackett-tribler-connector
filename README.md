# Jackett to Tribler Connector

## Description

This script is tasked with periodically fetching torrent information from various trackers via Jackett, and feeding this information to Tribler so as to add it to its Channels. The script also periodically commits the Channel changes, as Tribler does not implicitly do this when a new torrent is added. 

## How it works

The script will periodically forward HTTP requests to a running Jackett service, querying it for tracker feeds. The Jackett service may be either local or remote. Jackett will return the feed in a RSS XML like format called Torznab. This script will then parse this XML in order to extract the magnet links embedded in the XML for each torrent. For each of the aforementioned magnet links, the script will forward an HTTP `PUT` request to Tribler's REST API towards the `mychannel/torrents` endpoint, which demands that the torrent be added. 

Asynchronous to this, the script will also periodically forward a commit request to Tribler's REST API towards the `mychannel/commit` endpoint, in order to demand that the Channel changes be committed. This is an HTTP `POST` request.

## Utilization Instructions

**Before running the script, it is required that both Jackett and Tribler services are running, and can be accessed via HTTP. Moreover, prior to running the script, Jackett must index the trackers we aim to keep track of, otherwise, the queries to Jackett will not return anything useful.**

The script offers a Command Line interface. It features the following parameters:

##### Mandatory:

* `api_key`: the API Key used by Jackett.
* `trackers`: a space separated list of trackers as requested by Jackett. Usually Jackett uses a concatenated lowercase version of the true tracker name, for instance: *Horrible Subs* → *horriblesubs*.

##### Optional:

* `jackett_ip`: the interface at which the Jackett service can be reached via HTTP. Defaults to: `localhost`.
* `jackett_port`: the port at which the Jackett service can be reached via HTTP. Defaults to: `9117`.
* `tribler_ip`: the interface at which Tribler can be reached via HTTP. Defaults to: `localhost`.
* `tribler_port`: the port at which Tribler can be reached via HTTP. Defaults to: `8085`.
* `query_interval`: the time interval (in seconds) between requests to Jackett. Defaults to: `600`.
* `commit_interval`: the time interval (in seconds) between channel commit requests to Tribler. Defaults to: `3600`.

##### Example Command Line:

`python jackett_script.py n2kvbic1q4isgtukcqnscvnom5rwrfz1 kickasstorrent rarbg horriblesubs --query_interval 45 --commit_interval 90`

Here, `n2kvbic1q4isgtukcqnscvnom5rwrfz1` is Jackett's API Key, `kickasstorrent rarbg horriblesubs` represents the list of trackers whose feed is retrieved via Jackett. `query_interval` will be set to `45 seconds` and `commit_interval` will be set to `90`. All other parameters will inherit their default values.

## Dependencies:

The script relies heavily on asynchronous coroutines. It requires the following to work properly:

* Python 3.5.3+ (tested on Python 3.6.8)
* AIOHTTP



