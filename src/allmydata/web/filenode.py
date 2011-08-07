
import simplejson

from twisted.web import http, static
from twisted.internet import defer
from nevow import url, rend
from nevow.inevow import IRequest

from allmydata.interfaces import ExistingChildError, SDMF_VERSION, MDMF_VERSION
from allmydata.monitor import Monitor
from allmydata.immutable.upload import FileHandle
from allmydata.mutable.publish import MutableFileHandle
from allmydata.mutable.common import MODE_READ
from allmydata.util import log, base32

from allmydata.web.common import text_plain, WebError, RenderMixin, \
     boolean_of_arg, get_arg, should_create_intermediate_directories, \
     MyExceptionHandler, parse_replace_arg, parse_offset_arg, \
     parse_mutable_type_arg
from allmydata.web.check_results import CheckResults, \
     CheckAndRepairResults, LiteralCheckResults
from allmydata.web.info import MoreInfo

class ReplaceMeMixin:
    def replace_me_with_a_child(self, req, client, replace):
        # a new file is being uploaded in our place.
        mutable = boolean_of_arg(get_arg(req, "mutable", "false"))
        if mutable:
            arg = get_arg(req, "mutable-type", None)
            mutable_type = parse_mutable_type_arg(arg)
            if mutable_type is "invalid":
                raise WebError("Unknown type: %s" % arg, http.BAD_REQUEST)

            data = MutableFileHandle(req.content)
            d = client.create_mutable_file(data, version=mutable_type)
            def _uploaded(newnode):
                d2 = self.parentnode.set_node(self.name, newnode,
                                              overwrite=replace)
                d2.addCallback(lambda res: newnode)
                return d2
            d.addCallback(_uploaded)
        else:
            uploadable = FileHandle(req.content, convergence=client.convergence)
            d = self.parentnode.add_file(self.name, uploadable,
                                         overwrite=replace)
        def _done(filenode):
            log.msg("webish upload complete",
                    facility="tahoe.webish", level=log.NOISY, umid="TCjBGQ")
            if self.node:
                # we've replaced an existing file (or modified a mutable
                # file), so the response code is 200
                req.setResponseCode(http.OK)
            else:
                # we've created a new file, so the code is 201
                req.setResponseCode(http.CREATED)
            return filenode.get_uri()
        d.addCallback(_done)
        return d

    def replace_me_with_a_childcap(self, req, client, replace):
        req.content.seek(0)
        childcap = req.content.read()
        childnode = client.create_node_from_uri(childcap, None, name=self.name)
        d = self.parentnode.set_node(self.name, childnode, overwrite=replace)
        d.addCallback(lambda res: childnode.get_uri())
        return d


    def replace_me_with_a_formpost(self, req, client, replace):
        # create a new file, maybe mutable, maybe immutable
        mutable = boolean_of_arg(get_arg(req, "mutable", "false"))

        # create an immutable file
        contents = req.fields["file"]
        if mutable:
            arg = get_arg(req, "mutable-type", None)
            mutable_type = parse_mutable_type_arg(arg)
            if mutable_type is "invalid":
                raise WebError("Unknown type: %s" % arg, http.BAD_REQUEST)
            uploadable = MutableFileHandle(contents.file)
            d = client.create_mutable_file(uploadable, version=mutable_type)
            def _uploaded(newnode):
                d2 = self.parentnode.set_node(self.name, newnode,
                                              overwrite=replace)
                d2.addCallback(lambda res: newnode.get_uri())
                return d2
            d.addCallback(_uploaded)
            return d

        uploadable = FileHandle(contents.file, convergence=client.convergence)
        d = self.parentnode.add_file(self.name, uploadable, overwrite=replace)
        d.addCallback(lambda newnode: newnode.get_uri())
        return d


class PlaceHolderNodeHandler(RenderMixin, rend.Page, ReplaceMeMixin):
    def __init__(self, client, parentnode, name):
        rend.Page.__init__(self)
        self.client = client
        assert parentnode
        self.parentnode = parentnode
        self.name = name
        self.node = None

    def render_PUT(self, ctx):
        req = IRequest(ctx)
        t = get_arg(req, "t", "").strip()
        replace = parse_replace_arg(get_arg(req, "replace", "true"))

        assert self.parentnode and self.name
        if req.getHeader("content-range"):
            raise WebError("Content-Range in PUT not yet supported",
                           http.NOT_IMPLEMENTED)
        if not t:
            return self.replace_me_with_a_child(req, self.client, replace)
        if t == "uri":
            return self.replace_me_with_a_childcap(req, self.client, replace)

        raise WebError("PUT to a file: bad t=%s" % t)

    def render_POST(self, ctx):
        req = IRequest(ctx)
        t = get_arg(req, "t", "").strip()
        replace = boolean_of_arg(get_arg(req, "replace", "true"))
        if t == "upload":
            # like PUT, but get the file data from an HTML form's input field.
            # We could get here from POST /uri/mutablefilecap?t=upload,
            # or POST /uri/path/file?t=upload, or
            # POST /uri/path/dir?t=upload&name=foo . All have the same
            # behavior, we just ignore any name= argument
            d = self.replace_me_with_a_formpost(req, self.client, replace)
        else:
            # t=mkdir is handled in DirectoryNodeHandler._POST_mkdir, so
            # there are no other t= values left to be handled by the
            # placeholder.
            raise WebError("POST to a file: bad t=%s" % t)

        when_done = get_arg(req, "when_done", None)
        if when_done:
            d.addCallback(lambda res: url.URL.fromString(when_done))
        return d


class FileNodeHandler(RenderMixin, rend.Page, ReplaceMeMixin):
    def __init__(self, client, node, parentnode=None, name=None):
        rend.Page.__init__(self)
        self.client = client
        assert node
        self.node = node
        self.parentnode = parentnode
        self.name = name

    def childFactory(self, ctx, name):
        req = IRequest(ctx)
        if should_create_intermediate_directories(req):
            raise WebError("Cannot create directory '%s', because its "
                           "parent is a file, not a directory" % name)
        raise WebError("Files have no children, certainly not named '%s'"
                       % name)

    def render_GET(self, ctx):
        req = IRequest(ctx)
        t = get_arg(req, "t", "").strip()
        if not t:
            # just get the contents
            # the filename arrives as part of the URL or in a form input
            # element, and will be sent back in a Content-Disposition header.
            # Different browsers use various character sets for this name,
            # sometimes depending upon how language environment is
            # configured. Firefox sends the equivalent of
            # urllib.quote(name.encode("utf-8")), while IE7 sometimes does
            # latin-1. Browsers cannot agree on how to interpret the name
            # they see in the Content-Disposition header either, despite some
            # 11-year old standards (RFC2231) that explain how to do it
            # properly. So we assume that at least the browser will agree
            # with itself, and echo back the same bytes that we were given.
            filename = get_arg(req, "filename", self.name) or "unknown"
            d = self.node.get_best_readable_version()
            d.addCallback(lambda dn: FileDownloader(dn, filename))
            return d
        if t == "json":
            # We do this to make sure that fields like size and
            # mutable-type (which depend on the file on the grid and not
            # just on the cap) are filled in. The latter gets used in
            # tests, in particular.
            #
            # TODO: Make it so that the servermap knows how to update in
            # a mode specifically designed to fill in these fields, and
            # then update it in that mode.
            if self.node.is_mutable():
                d = self.node.get_servermap(MODE_READ)
            else:
                d = defer.succeed(None)
            if self.parentnode and self.name:
                d.addCallback(lambda ignored:
                    self.parentnode.get_metadata_for(self.name))
            else:
                d.addCallback(lambda ignored: None)
            d.addCallback(lambda md: FileJSONMetadata(ctx, self.node, md))
            return d
        if t == "info":
            return MoreInfo(self.node)
        if t == "uri":
            return FileURI(ctx, self.node)
        if t == "readonly-uri":
            return FileReadOnlyURI(ctx, self.node)
        raise WebError("GET file: bad t=%s" % t)

    def render_HEAD(self, ctx):
        req = IRequest(ctx)
        t = get_arg(req, "t", "").strip()
        if t:
            raise WebError("GET file: bad t=%s" % t)
        filename = get_arg(req, "filename", self.name) or "unknown"
        d = self.node.get_best_readable_version()
        d.addCallback(lambda dn: FileDownloader(dn, filename))
        return d

    def render_PUT(self, ctx):
        req = IRequest(ctx)
        t = get_arg(req, "t", "").strip()
        replace = parse_replace_arg(get_arg(req, "replace", "true"))
        offset = parse_offset_arg(get_arg(req, "offset", None))

        if not t:
            if not replace:
                # this is the early trap: if someone else modifies the
                # directory while we're uploading, the add_file(overwrite=)
                # call in replace_me_with_a_child will do the late trap.
                raise ExistingChildError()

            if self.node.is_mutable():
                # Are we a readonly filenode? We shouldn't allow callers
                # to try to replace us if we are.
                if self.node.is_readonly():
                    raise WebError("PUT to a mutable file: replace or update"
                                   " requested with read-only cap")
                if offset is None:
                    return self.replace_my_contents(req)

                if offset >= 0:
                    return self.update_my_contents(req, offset)

                raise WebError("PUT to a mutable file: Invalid offset")

            else:
                if offset is not None:
                    raise WebError("PUT to a file: append operation invoked "
                                   "on an immutable cap")

                assert self.parentnode and self.name
                return self.replace_me_with_a_child(req, self.client, replace)

        if t == "uri":
            if not replace:
                raise ExistingChildError()
            assert self.parentnode and self.name
            return self.replace_me_with_a_childcap(req, self.client, replace)

        raise WebError("PUT to a file: bad t=%s" % t)

    def render_POST(self, ctx):
        req = IRequest(ctx)
        t = get_arg(req, "t", "").strip()
        replace = boolean_of_arg(get_arg(req, "replace", "true"))
        if t == "check":
            d = self._POST_check(req)
        elif t == "upload":
            # like PUT, but get the file data from an HTML form's input field
            # We could get here from POST /uri/mutablefilecap?t=upload,
            # or POST /uri/path/file?t=upload, or
            # POST /uri/path/dir?t=upload&name=foo . All have the same
            # behavior, we just ignore any name= argument
            if self.node.is_mutable():
                d = self.replace_my_contents_with_a_formpost(req)
            else:
                if not replace:
                    raise ExistingChildError()
                assert self.parentnode and self.name
                d = self.replace_me_with_a_formpost(req, self.client, replace)
        else:
            raise WebError("POST to file: bad t=%s" % t)

        when_done = get_arg(req, "when_done", None)
        if when_done:
            d.addCallback(lambda res: url.URL.fromString(when_done))
        return d

    def _maybe_literal(self, res, Results_Class):
        if res:
            return Results_Class(self.client, res)
        return LiteralCheckResults(self.client)

    def _POST_check(self, req):
        verify = boolean_of_arg(get_arg(req, "verify", "false"))
        repair = boolean_of_arg(get_arg(req, "repair", "false"))
        add_lease = boolean_of_arg(get_arg(req, "add-lease", "false"))
        if repair:
            d = self.node.check_and_repair(Monitor(), verify, add_lease)
            d.addCallback(self._maybe_literal, CheckAndRepairResults)
        else:
            d = self.node.check(Monitor(), verify, add_lease)
            d.addCallback(self._maybe_literal, CheckResults)
        return d

    def render_DELETE(self, ctx):
        assert self.parentnode and self.name
        d = self.parentnode.delete(self.name)
        d.addCallback(lambda res: self.node.get_uri())
        return d

    def replace_my_contents(self, req):
        req.content.seek(0)
        new_contents = MutableFileHandle(req.content)
        d = self.node.overwrite(new_contents)
        d.addCallback(lambda res: self.node.get_uri())
        return d


    def update_my_contents(self, req, offset):
        req.content.seek(0)
        added_contents = MutableFileHandle(req.content)

        d = self.node.get_best_mutable_version()
        d.addCallback(lambda mv:
            mv.update(added_contents, offset))
        d.addCallback(lambda ignored:
            self.node.get_uri())
        return d


    def replace_my_contents_with_a_formpost(self, req):
        # we have a mutable file. Get the data from the formpost, and replace
        # the mutable file's contents with it.
        new_contents = req.fields['file']
        new_contents = MutableFileHandle(new_contents.file)

        d = self.node.overwrite(new_contents)
        d.addCallback(lambda res: self.node.get_uri())
        return d


class FileDownloader(rend.Page):
    def __init__(self, filenode, filename):
        rend.Page.__init__(self)
        self.filenode = filenode
        self.filename = filename

    def parse_range_header(self, range):
        # Parse a byte ranges according to RFC 2616 "14.35.1 Byte
        # Ranges".  Returns None if the range doesn't make sense so it
        # can be ignored (per the spec).  When successful, returns a
        # list of (first,last) inclusive range tuples.

        filesize = self.filenode.get_size()
        assert isinstance(filesize, (int,long)), filesize

        try:
            # byte-ranges-specifier
            units, rangeset = range.split('=', 1)
            if units != 'bytes':
                return None     # nothing else supported

            def parse_range(r):
                first, last = r.split('-', 1)

                if first is '':
                    # suffix-byte-range-spec
                    first = filesize - long(last)
                    last = filesize - 1
                else:
                    # byte-range-spec

                    # first-byte-pos
                    first = long(first)

                    # last-byte-pos
                    if last is '':
                        last = filesize - 1
                    else:
                        last = long(last)

                if last < first:
                    raise ValueError

                return (first, last)

            # byte-range-set
            #
            # Note: the spec uses "1#" for the list of ranges, which
            # implicitly allows whitespace around the ',' separators,
            # so strip it.
            return [ parse_range(r.strip()) for r in rangeset.split(',') ]
        except ValueError:
            return None

    def renderHTTP(self, ctx):
        req = IRequest(ctx)
        gte = static.getTypeAndEncoding
        ctype, encoding = gte(self.filename,
                              static.File.contentTypes,
                              static.File.contentEncodings,
                              defaultType="text/plain")
        req.setHeader("content-type", ctype)
        if encoding:
            req.setHeader("content-encoding", encoding)

        if boolean_of_arg(get_arg(req, "save", "False")):
            # tell the browser to save the file rather display it we don't
            # try to encode the filename, instead we echo back the exact same
            # bytes we were given in the URL. See the comment in
            # FileNodeHandler.render_GET for the sad details.
            req.setHeader("content-disposition",
                          'attachment; filename="%s"' % self.filename)

        filesize = self.filenode.get_size()
        assert isinstance(filesize, (int,long)), filesize
        first, size = 0, None
        contentsize = filesize
        req.setHeader("accept-ranges", "bytes")
        if not self.filenode.is_mutable():
            # TODO: look more closely at Request.setETag and how it interacts
            # with a conditional "if-etag-equals" request, I think this may
            # need to occur after the setResponseCode below
            si = self.filenode.get_storage_index()
            if si:
                req.setETag(base32.b2a(si))
        # TODO: for mutable files, use the roothash. For LIT, hash the data.
        # or maybe just use the URI for CHK and LIT.
        rangeheader = req.getHeader('range')
        if rangeheader:
            ranges = self.parse_range_header(rangeheader)

            # ranges = None means the header didn't parse, so ignore
            # the header as if it didn't exist.  If is more than one
            # range, then just return the first for now, until we can
            # generate multipart/byteranges.
            if ranges is not None:
                first, last = ranges[0]

                if first >= filesize:
                    raise WebError('First beyond end of file',
                                   http.REQUESTED_RANGE_NOT_SATISFIABLE)
                else:
                    first = max(0, first)
                    last = min(filesize-1, last)

                    req.setResponseCode(http.PARTIAL_CONTENT)
                    req.setHeader('content-range',"bytes %s-%s/%s" %
                                  (str(first), str(last),
                                   str(filesize)))
                    contentsize = last - first + 1
                    size = contentsize

        req.setHeader("content-length", str(contentsize))
        if req.method == "HEAD":
            return ""

        # Twisted >=9.0 throws an error if we call req.finish() on a closed
        # HTTP connection. It also has req.notifyFinish() to help avoid it.
        finished = []
        def _request_finished(ign):
            finished.append(True)
        if hasattr(req, "notifyFinish"):
            req.notifyFinish().addBoth(_request_finished)

        d = self.filenode.read(req, first, size)

        def _finished(ign):
            if not finished:
                req.finish()
        def _error(f):
            lp = log.msg("error during GET", facility="tahoe.webish", failure=f,
                         level=log.UNUSUAL, umid="xSiF3w")
            if finished:
                log.msg("but it's too late to tell them", parent=lp,
                        level=log.UNUSUAL, umid="j1xIbw")
                return
            req._tahoe_request_had_error = f # for HTTP-style logging
            if req.startedWriting:
                # The content-type is already set, and the response code has
                # already been sent, so we can't provide a clean error
                # indication. We can emit text (which a browser might
                # interpret as something else), and if we sent a Size header,
                # they might notice that we've truncated the data. Keep the
                # error message small to improve the chances of having our
                # error response be shorter than the intended results.
                #
                # We don't have a lot of options, unfortunately.
                req.write("problem during download\n")
                req.finish()
            else:
                # We haven't written anything yet, so we can provide a
                # sensible error message.
                eh = MyExceptionHandler()
                eh.renderHTTP_exception(ctx, f)
        d.addCallbacks(_finished, _error)
        return req.deferred


def FileJSONMetadata(ctx, filenode, edge_metadata):
    rw_uri = filenode.get_write_uri()
    ro_uri = filenode.get_readonly_uri()
    data = ("filenode", {})
    data[1]['size'] = filenode.get_size()
    if ro_uri:
        data[1]['ro_uri'] = ro_uri
    if rw_uri:
        data[1]['rw_uri'] = rw_uri
    verifycap = filenode.get_verify_cap()
    if verifycap:
        data[1]['verify_uri'] = verifycap.to_string()
    data[1]['mutable'] = filenode.is_mutable()
    if edge_metadata is not None:
        data[1]['metadata'] = edge_metadata

    if filenode.is_mutable() and filenode.get_version() is not None:
        mutable_type = filenode.get_version()
        assert mutable_type in (MDMF_VERSION, SDMF_VERSION)
        if mutable_type == MDMF_VERSION:
            mutable_type = "mdmf"
        else:
            mutable_type = "sdmf"
        data[1]['mutable-type'] = mutable_type

    return text_plain(simplejson.dumps(data, indent=1) + "\n", ctx)

def FileURI(ctx, filenode):
    return text_plain(filenode.get_uri(), ctx)

def FileReadOnlyURI(ctx, filenode):
    if filenode.is_readonly():
        return text_plain(filenode.get_uri(), ctx)
    return text_plain(filenode.get_readonly_uri(), ctx)

class FileNodeDownloadHandler(FileNodeHandler):
    def childFactory(self, ctx, name):
        return FileNodeDownloadHandler(self.client, self.node, name=name)
