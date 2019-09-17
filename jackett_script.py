import asyncio
import aiohttp
import logging
import re

from xml.etree import ElementTree

# Matches a comma separated list of numbers, or the empty string

CATEGORY_STRING = re.compile("(^([0-9]+,)*[0-9]+$|(^$))")


class JackettRequestConstruction:

    def __init__(self, ip, port, api_key):
        """
        Initialize a JackettRequestConstruction object.

        :param ip: the IP where the Jackett service is located.
        :param port: the port where the Jackett service is located.
        :param api_key: the API Key of the Jackett service.
        """
        self.ip = ip
        self.port = port
        self.api_key = api_key

    def _jackett_url_constructor(self, query_type="caps", tracker="all", **kwargs):
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

    def caps_jackett_request(self, tracker="all"):
        """
        Create a CAPS type Torznab request

        :return: a CAPS request
        """
        return self._jackett_url_constructor(tracker=tracker)

    def search_jackett_request(self, q="", limit=50, cat="", maxage=100, offset=0, tracker="all"):
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
        assert CATEGORY_STRING.match(cat), "The cat parameter is incorrectly formatted"

        return self._jackett_url_constructor(query_type="search", tracker=tracker, q=q, limit=limit, cat=cat,
                                             maxage=maxage, offset=offset)

    def get_tracker_feed(self, tracker):
        """
        Get the raw feed of a particular tracker. This is a shorthand method for a SEARCH operation with no category
        filtering or query phrase.

        :param tracker: the name of the tracker whose feed should be fetched
        :return: a SEARCH request, which fetches the tracker's feed
        """
        return self._jackett_url_constructor(query_type="search", tracker=tracker)


class JackettFeedParser:

    # This dictionary is required by Element.find() (from ElementTree.Element) in order to correctly identify tags
    RSS_TORZNAB_NAMESPACES = {
        'torznab': 'http://torznab.com/schemas/2015/feed'
    }

    def __init__(self, ip, port, api_key, tracker, request_interval=60):
        """
        Initialize a JackettFeedParser object.

        :param ip: the interface on which the Jackett service is running.
        :param port: the port on which the Jackett service is running.
        :param api_key: the API Key of the Jackett service.
        :param tracker: the name of the tracker whose feed this parser monitors.
        :param request_interval: the interval (in seconds) between requests to the jackett service
        """
        self._jackett_req_constructor = JackettRequestConstruction(ip, port, api_key)
        self._tracker = tracker
        self._request_interval = request_interval
        self._task = None

        self._logger = logging.getLogger(self.__class__.__name__)
        # TODO: The initializer should accept either an object to the Endpoint, where the addition of the torrent is
        #       done there should be a url where the request should be sent to add the new torrents
        # TODO: add the code which parses the XML, looks for the link, and then forwards the link to a method which
        #       adds it to the torrent list. Look into the existing code for this, and check to see if there are
        #       specialized methods which are focused on adding the torrent from a magnet link or a link to a .torrent
        #       download.
        # TODO:  check to see how this code handles non-valid XMLs. Potentially even badly formed ones.

    def set_request_interval(self, interval):
        """
        Set the request_interval field.

        :param interval: the new request interval
        """
        self._request_interval = interval

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
        if item.tag != 'item':
            return None

        for tag in item.findall('torznab:attr', self.RSS_TORZNAB_NAMESPACES):
            if tag.get('name') == name and tag.get('value'):
                return tag.get('value')

        return None

    def _parse_links(self, input):
        """
        Retrieve the torrent links from the XML input, and return them in a dictionary.

        :param input: the input string should contain a valid RSS XML.
        :return: a dictionary, which points from the torrent name to the torrent (magnet-)link
        """
        # FIXME: Should I return silently if the XML is not well formed, or should I propagate the error up?
        try:
            rss_xml = ElementTree.fromstring(input)
        except ElementTree.ParseError as exc:
            self._logger.error("The RSS XML is not well formed: {}".format(exc))
            return

        # This is where the torrent links will be stored
        torrent_dict = {}

        for item in rss_xml.iter('item'):
            # try to get the infohash first
            key = self._get_torznab_attribute(item, "infohash")

            if not key and item.find('title'):
                key = item.find('title').text

            if key:
                val = self._get_torznab_attribute(item, "magneturl")

                if not val and item.find('link'):
                    val = item.find('link').text

                torrent_dict[key] = val

        return torrent_dict

    async def _get(self, session, url):
        """
        Forward an HTTP request, and await its response.

        :param session: an aiohttp.ClientSession object
        :param url: the url where the HTTP request is to be forwarded
        :return: the response in string format
        """
        async with session.get(url) as response:
            return await response.text()

    async def _loop_requests(self):
        """
        Forward requests to Jackett in order to fetch the feed of the targeted tracker. This is done forever until the
        task is stopped via the stop() method. The time difference between each request is _request_interval.
        """
        # Loop until the task is closed
        while True:
            # FIXME: There should only be one session per application; see here: https://docs.aiohttp.org/en/stable/client_quickstart.html#make-a-request
            async with aiohttp.ClientSession() as session:
                    # Forward a request for the RSS feed
                    response = await self._get(session,
                                               self._jackett_req_constructor.get_tracker_feed(tracker=self._tracker))

                    # Parse the response, and get the infohash -> magnetlink dict
                    res = self._parse_links(response)
                    print(res)

                    # Forward a request for each of the magnetlinks asynchronously

            await asyncio.sleep(self._request_interval)

    def start(self):
        """
        Start the JackettFeedParser object. After this method is called, a request will be periodically forwarded to
        the Jackett service in order to request the feed for the Parser's tracker.
        """
        if self._task:
            return

        loop = asyncio.get_event_loop()

        self._task = loop.create_task(self._loop_requests())

    def stop(self):
        """
        Stops the requests to the Jackett service.
        """
        self._task.cancel()
        self._task = None


async def _main():
    # Create a request constructor object
    jackett_req_constructor = JackettFeedParser("localhost", 9117, "b1khzic1q4ixkzukcqnscvnom6pkrfz6", "rarbg",
                                                request_interval=5)

    jackett_req_constructor.start()

    await asyncio.sleep(40)

    jackett_req_constructor.stop()

    await asyncio.sleep(40)

    jackett_req_constructor.start()

    await asyncio.sleep(40)


def main():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(_main())
    loop.close()


if __name__ == '__main__':
    main()
