import aiohttp
import asyncio
import logging
import re
import sys

from threading import Thread
from xml.etree import ElementTree

# Matches a comma separated list of numbers, or the empty string
CATEGORY_STRING = re.compile("(^([0-9]+,)*[0-9]+$|(^$))")


class JackettRequestConstructor:

    def __init__(self, ip, port, api_key):
        """
        Initialize a JackettRequestConstructor object.

        :param ip: the IP where the Jackett service is located.
        :param port: the port where the Jackett service is located.
        :param api_key: the API Key of the Jackett service.
        """
        self.ip = ip
        self.port = port
        self.api_key = api_key

    def _url_constructor(self, query_type="caps", tracker="all", **kwargs):
        """
        Build a URL for sending requests to a running Jackett service.

        :param query_type: the type of the query (must be one which is accepted by Torznab). Defaults to 'caps'.
        :param tracker: the tracker for which the request is forwarded. Defaults to 'all'.
        :param kwargs: additional arguments required by the query_type.
        :return: returns a URL which can be used to reach the Jackett service.
        """
        url = "http://{}:{}/api/v2.0/indexers/{}/results/torznab/api?t={}&apikey={}".format(self.ip, self.port, tracker,
                                                                                            query_type, self.api_key)

        return url + "".join(["&{}={}".format(k, v) for k, v in kwargs.items()])

    def caps_request(self, tracker="all"):
        """
        Create a CAPS type Torznab request

        :return: a CAPS request
        """
        return self._url_constructor(tracker=tracker)

    def search_request(self, q="", limit=50, cat="", maxage=100, offset=0, tracker="all"):
        """
        Create a SEARCH type Torznab request.

        :param q: the query string.
        :param limit: the maximal number of results to be retrieved.
        :param cat: a string of comma (',') separated categories ids. For example: "200,300,400"
        :param maxage: results older than maxage days are dropped
        :param offset: results fetched before the offset's value are dropped
        :param tracker: the tracker for which the request is forwarded. Defaults to 'all'.
        :return: a SEARCH request
        """
        if not CATEGORY_STRING.match(cat):
            raise ValueError("The cat parameter is incorrectly formatted")

        return self._url_constructor(query_type="search", tracker=tracker, q=q, limit=limit, cat=cat,
                                     maxage=maxage, offset=offset)

    def get_tracker_feed(self, tracker):
        """
        Get the raw feed of a particular tracker. This is a shorthand method for a SEARCH operation with no category
        filtering or query phrase.

        :param tracker: the name of the tracker whose feed should be fetched
        :return: a SEARCH request, which fetches the tracker's feed
        """
        return self._url_constructor(query_type="search", tracker=tracker)


class TriblerRequestConstructor:

    def __init__(self, ip, port):
        self._base_url = "http://{}:{}/".format(ip, port)

    def _url_constructor(self, endpoint):
        """
        Create a URL for sending requests to one of Tribler's endpoints

        :param endpoint: either a string which represents the endpoint, or a list of strings which are concatenated
                         together (separated by '/').
        :return: a URL which points to a Tribler endpoint
        """
        if not endpoint:
            raise ValueError("The endpoint parameter cannot be None")
        elif not isinstance(endpoint, (str, list)):
            raise ValueError("The endpoint parameter must either be a string or a list of strings")
        elif isinstance(endpoint, list) and any([not isinstance(x, str) for x in endpoint]):
            raise ValueError("The endpoint list must only contain strings")

        return self._base_url + (endpoint if isinstance(endpoint, str) else '/'.join(endpoint))

    def _url_constructor_with_args(self, endpoint, **kwargs):
        """
        Create a URL for sending requests to one of Tribler's endpoints, which contains parmeters embeded in its

        :param endpoint: either a string which represents the endpoint, or a list of strings which are concatenated
                         together (separated by '/').
        :param kwargs: this is where all the extra parameters will be stored
        :return: a URL which points to a Tribler endpoint
        """
        return self._url_constructor(endpoint) + "".join(["&{}={}".format(k, v) for k, v in kwargs.items()])

    def add_torrent_request(self):
        """
        Generate a URL for the request of adding a new torrent. Since this is a PUT HTTP request, the parameters are
        not encoded in the URL, and will have to be added in the request body separately.

        :return: a URL for requesting the addition of a new torrent.
        """
        return self._url_constructor(['mychannel', 'torrents'])

    def commit_torrents_request(self):
        """
        Generate a URL to request that new torrents be committed. This is a POST HTTP request.

        :return: a URL for committing new torrents.
        """
        return self._url_constructor(['mychannel', 'commit'])


class JackettFeedParser:
    # This dictionary is required by Element.find() (from ElementTree.Element) in order to correctly identify tags
    RSS_TORZNAB_NAMESPACES = {
        'torznab': 'http://torznab.com/schemas/2015/feed'
    }

    def __init__(self, jackett_ip, jackett_port, api_key, tracker, tribler_ip, tribler_port, request_interval=600,
                 commit_interval=3600):
        """
        Initialize a JackettFeedParser object.

        :param jackett_ip: the interface on which the Jackett service is running.
        :param jackett_port: the port on which the Jackett service is running.
        :param api_key: the API Key of the Jackett service.
        :param tracker: the name of the tracker whose feed this parser monitors.
        :param tribler_ip: the interface on which the Tribler service is running.
        :param tribler_port: the port on which the Tribler service is running.
        :param request_interval: the interval (in seconds) between requests to the jackett service
        :param commit_interval: the interval (in seconds) between requests to Tribler for committing the torrents
        """
        self._jackett_req_constructor = JackettRequestConstructor(jackett_ip, jackett_port, api_key)
        self._tribler_req_constructor = TriblerRequestConstructor(tribler_ip, tribler_port)

        self._tracker = tracker
        self._request_interval = request_interval
        self._commit_interval = commit_interval
        self._request_task = None
        self._commit_task = None

        self._session = None

        self._logger = logging.getLogger(self.__class__.__name__)

    def _get_torznab_attribute(self, item, name):
        """
        Retrieve the value of the Torznab attribute under the indicated name. If this attribute does not exist,
        None is returned.

        :param item: the rss_xml object. This should be an Element object, which points to an item tag.
        :param name: the name of the torznab attribute. More specifically, this is the value of the name attribute in
                     a torznab:attr tag.
        :return: the value attribute as a string, if the searched attribute exists, or None.
        """
        # A bit of defensive programming: check to see that the
        if not isinstance(item, ElementTree.Element) or item.tag != 'item':
            raise ValueError("The item parameter is either not of Element type or does not point to an <item> tag")

        for tag in item.findall('torznab:attr', self.RSS_TORZNAB_NAMESPACES):
            if tag.get('name') == name and tag.get('value'):
                return tag.get('value')

        return None

    def _parse_links(self, input):
        """
        Retrieve the torrent links from the XML input, and return them in a dictionary.

        :param input: the input string should contain a valid RSS XML.
        :return: a dictionary, which points from the torrent infohash or name to the torrent (magnet-)link
        """
        rss_xml = ElementTree.fromstring(input)

        # This is where the torrent links will be stored
        torrent_dict = {}

        for item in rss_xml.iter('item'):
            # try to get the infohash first if that fails, get the title
            key = self._get_torznab_attribute(item, "infohash")

            if (not key) and item.find('title') is not None:
                key = item.find('title').text

            if key:
                val = self._get_torznab_attribute(item, "magneturl")

                if not val and item.find('link') is not None:
                    val = item.find('link').text

                torrent_dict[key] = val

        return torrent_dict

    async def _get(self, url):
        """
        Forward an HTTP GET request, and await its response.

        :param url: the url where the HTTP GET request is to be forwarded
        :return: the response in string format
        """
        async with self._session.get(url) as response:
            return await response.text()

    async def _put(self, url, data=None):
        """
        Forward an HTTP PUT request, and await its response.

        :param url: the url where the HTTP PUT request is to be forwarded
        :param data: a dictionary, bytes, or a file-like object which is to be sent in the body of the request. This
                     parameter may also be None
        :return: the response in string format
        """
        async with self._session.put(url, data=data) as response:
            return await response.text()

    async def _post(self, url, data=None):
        """
        Forward an HTTP POST request, and await its response.

        :param url: the url where the HTTP POST request is to be forwarded
        :param data: a dictionary, bytes, or a file-like object which is to be sent in the body of the request. This
                     parameter may also be None
        :return: the response in string format
        """
        async with self._session.post(url, data=data) as response:
            return await response.text()

    async def _add_torrents(self, torrents, chunk_size=100):
        """
        Forward requests for adding each of the retrieved torrent from the Jackett service to Tribler.

        :param torrents: a dictionary of torrents, each entry points from the torrent's infohash to its magnet link
        :param chunk_size: the number of torrents that should be added at once
        """
        keys = list(torrents.keys())
        results = []

        for idx in range(0, len(keys), chunk_size):
            coros = [self._put(self._tribler_req_constructor.add_torrent_request(),
                               {'uri': torrents[key]}) for key in keys[idx:idx + chunk_size]]

            temp_results = await asyncio.gather(*coros)

            results += temp_results

        return results

    async def _loop_requests(self):
        """
        Forward requests to Jackett in order to fetch the feed of the targeted tracker. This is done forever until the
        task is stopped via the stop() method. The time difference between each request is _request_interval.
        """
        # Loop until the task is closed
        while True:
            # Forward a request for the Torznab RSS feed
            raw_response = await self._get(self._jackett_req_constructor.get_tracker_feed(tracker=self._tracker))

            try:
                # Get the torrent magnet links
                torrent_links = self._parse_links(raw_response)
                print(torrent_links[list(torrent_links.keys())[0]])
            except ElementTree.ParseError as exc:
                print(exc, file=sys.stderr)
            else:
                # Forward torrent addition requests to Tribler
                addition_results = await self._add_torrents(torrent_links)
                print(addition_results)

            # Wait some time until forwarding a new request
            await asyncio.sleep(self._request_interval)

    async def _loop_commit(self):
        """
        Forward torrent commit requests to Tribler. This is done forever until the task is stopped bia the stop()
        method. The time difference between each request is _commit_interval.
        """
        while True:
            await self._post(self._tribler_req_constructor.commit_torrents_request())

            await asyncio.sleep(self._commit_interval)

    def start(self):
        """
        Start the JackettFeedParser object. After this method is called, a request will be periodically forwarded to
        the Jackett service in order to request the feed for the Parser's tracker.
        """
        if self._request_task:
            return

        self._session = aiohttp.ClientSession()

        loop = asyncio.get_event_loop()

        self._request_task = loop.create_task(self._loop_requests())
        self._commit_task = loop.create_task(self._loop_commit())

    async def stop(self):
        """
        Stop the requests to the Jackett and Tribler services.
        """
        # Close the ongoing asyncio tasks
        self._request_task.cancel()
        self._request_task = None

        self._commit_task.cancel()
        self._commit_task = None

        # Close the aiohttp session
        await self._session.close()


async def _close_loop(parser):
    """
    Await for the return key to be pressed, then shut down the parser gracefully.

    :param parser: the parser object which reads input from Jackett and feeds it to Tribler
    """
    input("Press <return> to stop")
    await parser.stop()


def close_loop(loop, parser):
    """
    This method should run in another thread. This method takes care of coordinating the termination of the torrent
    addition process, and of terminating the loops

    :param loop: the main thread's event loop
    :param parser: the parser object which reads input from Jackett and feeds it to Tribler
    """
    new_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(new_loop)

    new_loop.run_until_complete(_close_loop(parser))

    new_loop.stop()
    loop.call_soon_threadsafe(loop.stop)


def main():
    """
    Starts the torrent retrieval and dissemination process. Also spawns a thread which is tasked with terminating the
    script gracefully when the return key is pressed.
    """

    # Configure these to your needs
    jackett_ip = "localhost"
    jackett_port = 9117

    api_key = "<API_Key>"
    tracker = "<tracker>"

    tribler_ip = "localhost"
    tribler_port = 8085

    # Launch the script
    jackett_parser = JackettFeedParser(jackett_ip, jackett_port, api_key, tracker, tribler_ip, tribler_port)

    jackett_parser.start()

    loop = asyncio.get_event_loop()

    termination_thread = Thread(target=close_loop, args=(loop, jackett_parser))
    termination_thread.start()

    loop.run_forever()

    termination_thread.join()

    print("The script has been terminated.")


if __name__ == '__main__':
    main()
