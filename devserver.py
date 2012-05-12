#!/usr/bin/python

# Copyright (c) 2009-2012 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""A CherryPy-based webserver to host images and build packages."""

import cherrypy
import cStringIO
import logging
import optparse
import os
import re
import sys
import subprocess
import tempfile

import autoupdate
import devserver_util
import downloader


CACHED_ENTRIES = 12

# Sets up global to share between classes.
global updater
updater = None


class DevServerError(Exception):
  """Exception class used by this module."""
  pass


def _LeadingWhiteSpaceCount(string):
  """Count the amount of leading whitespace in a string.

  Args:
    string: The string to count leading whitespace in.
  Returns:
    number of white space chars before characters start.
  """
  matched = re.match('^\s+', string)
  if matched:
    return len(matched.group())

  return 0


def _PrintDocStringAsHTML(func):
  """Make a functions docstring somewhat HTML style.

  Args:
    func: The function to return the docstring from.
  Returns:
    A string that is somewhat formated for a web browser.
  """
  # TODO(scottz): Make this parse Args/Returns in a prettier way.
  # Arguments could be bolded and indented etc.
  html_doc = []
  for line in func.__doc__.splitlines():
    leading_space = _LeadingWhiteSpaceCount(line)
    if leading_space > 0:
      line = '&nbsp;' * leading_space + line

    html_doc.append('<BR>%s' % line)

  return '\n'.join(html_doc)


def _GetConfig(options):
  """Returns the configuration for the devserver."""
  base_config = { 'global':
                  { 'server.log_request_headers': True,
                    'server.protocol_version': 'HTTP/1.1',
                    'server.socket_host': '::',
                    'server.socket_port': int(options.port),
                    'server.socket_timeout': 6000,
                    'response.timeout': 6000,
                    'tools.staticdir.root':
                      os.path.dirname(os.path.abspath(sys.argv[0])),
                  },
                  '/api':
                  {
                    # Gets rid of cherrypy parsing post file for args.
                    'request.process_request_body': False,
                  },
                  '/build':
                  {
                    'response.timeout': 100000,
                  },
                  '/update':
                  {
                    # Gets rid of cherrypy parsing post file for args.
                    'request.process_request_body': False,
                    'response.timeout': 10000,
                  },
                  # Sets up the static dir for file hosting.
                  '/static':
                  { 'tools.staticdir.dir': 'static',
                    'tools.staticdir.on': True,
                    'response.timeout': 10000,
                  },
                }

  if options.log_dir:
    base_config['global']['log.access_file'] = os.path.join(
        options.log_dir, 'devserver_access.log')
    base_config['global']['log.error_file'] = os.path.join(
        options.log_dir, 'devserver_error.log')

  if options.production:
    base_config['global']['server.environment'] = 'production'

  return base_config


def _PrepareToServeUpdatesOnly(image_dir, static_dir):
  """Sets up symlink to image_dir for serving purposes."""
  assert os.path.exists(image_dir), '%s must exist.' % image_dir
  # If  we're  serving  out  of  an archived  build  dir  (e.g.  a
  # buildbot), prepare this webserver's magic 'static/' dir with a
  # link to the build archive.
  cherrypy.log('Preparing autoupdate for "serve updates only" mode.',
               'DEVSERVER')
  if os.path.lexists('%s/archive' % static_dir):
    if image_dir != os.readlink('%s/archive' % static_dir):
      cherrypy.log('removing stale symlink to %s' % image_dir, 'DEVSERVER')
      os.unlink('%s/archive' % static_dir)
      os.symlink(image_dir, '%s/archive' % static_dir)
  else:
    os.symlink(image_dir, '%s/archive' % static_dir)
  cherrypy.log('archive dir: %s ready to be used to serve images.' % image_dir,
               'DEVSERVER')


class ApiRoot(object):
  """RESTful API for Dev Server information."""
  exposed = True

  @cherrypy.expose
  def hostinfo(self, ip):
    """Returns a JSON dictionary containing information about the given ip.

    Not all information may be known at the time the request is made. The
    possible keys are:

        last_event_type: int
            Last update event type received.

        last_event_status: int
            Last update event status received.

        last_known_version: string
            Last known version recieved for update ping.

        forced_update_label: string
            Update label to force next update ping to use. Set by setnextupdate.

    See the OmahaEvent class in update_engine/omaha_request_action.h for status
    code definitions. If the ip does not exist an empty string is returned."""
    return updater.HandleHostInfoPing(ip)

  @cherrypy.expose
  def hostlog(self, ip):
    """Returns a JSON object containing a log of events pertaining to a
    particular host, or all hosts. Log events contain a timestamp and any
    subset of the attributes listed for the hostinfo method."""
    return updater.HandleHostLogPing(ip)

  @cherrypy.expose
  def setnextupdate(self, ip):
    """Allows the response to the next update ping from a host to be set.

    Takes the IP of the host and an update label as normally provided to the
    /update command."""
    body_length = int(cherrypy.request.headers['Content-Length'])
    label = cherrypy.request.rfile.read(body_length)

    if label:
      label = label.strip()
      if label:
        return updater.HandleSetUpdatePing(ip, label)
    raise cherrypy.HTTPError(400, 'No label provided.')


class DevServerRoot(object):
  """The Root Class for the Dev Server.

  CherryPy works as follows:
    For each method in this class, cherrpy interprets root/path
    as a call to an instance of DevServerRoot->method_name.  For example,
    a call to http://myhost/build will call build.  CherryPy automatically
    parses http args and places them as keyword arguments in each method.
    For paths http://myhost/update/dir1/dir2, you can use *args so that
    cherrypy uses the update method and puts the extra paths in args.
  """
  api = ApiRoot()

  def __init__(self):
    self._builder = None
    self._downloader_dict = {}

  @cherrypy.expose
  def build(self, board, pkg, **kwargs):
    """Builds the package specified."""
    import builder
    if self._builder is None:
      self._builder = builder.Builder()
    return self._builder.Build(board, pkg, kwargs)

  @cherrypy.expose
  def download(self, **kwargs):
    """Downloads and archives full/delta payloads from Google Storage.

    This methods downloads artifacts. It may download artifacts in the
    background in which case a caller should call wait_for_status to get
    the status of the background artifact downloads. They should use the same
    args passed to download.

    Args:
      archive_url: Google Storage URL for the build.

    Example URL:
      'http://myhost/download?archive_url=gs://chromeos-image-archive/'
      'x86-generic/R17-1208.0.0-a1-b338'
    """
    downloader_instance = downloader.Downloader(updater.static_dir)
    archive_url = kwargs.get('archive_url')
    if not archive_url:
      raise DevServerError("Didn't specify the archive_url in request")

    # Do this before we start such that other calls to the downloader or
    # wait_for_status are blocked until this completed/failed.
    self._downloader_dict[archive_url] = downloader_instance
    try:
      return_obj = downloader_instance.Download(archive_url, background=True)
    except:
      self._downloader_dict[archive_url] = None
      raise

    return return_obj

  @cherrypy.expose
  def stage_debug(self, **kwargs):
    """Downloads and stages debug symbol payloads from Google Storage.

    This methods downloads the debug symbol build artifact synchronously,
    and then stages it for use by symbolicate_dump/.

    Args:
      archive_url: Google Storage URL for the build.

    Example URL:
      'http://myhost/stage_debug?archive_url=gs://chromeos-image-archive/'
      'x86-generic/R17-1208.0.0-a1-b338'
    """
    archive_url = kwargs.get('archive_url')
    if not archive_url:
      raise DevServerError("Didn't specify the archive_url in request")

    return downloader.SymbolDownloader(updater.static_dir).Download(archive_url)

  @cherrypy.expose
  def symbolicate_dump(self, minidump):
    """Symbolicates a minidump using pre-downloaded symbols, returns it.

    Callers will need to POST to this URL with a body of MIME-type
    "multipart/form-data".
    The body should include a single argument, 'minidump', containing the
    binary-formatted minidump to symbolicate.

    It is up to the caller to ensure that the symbols they want are currently
    staged.

    Args:
      minidump: The binary minidump file to symbolicate.
    """
    to_return = ''
    with tempfile.NamedTemporaryFile() as local:
      while True:
        data = minidump.file.read(8192)
        if not data:
          break
        local.write(data)
      local.flush()
      stackwalk = subprocess.Popen(['minidump_stackwalk',
                                    local.name,
                                    updater.static_dir + '/debug/breakpad'],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
      to_return, error_text = stackwalk.communicate()
      if stackwalk.returncode != 0:
        raise DevServerError("Can't generate stack trace: %s (rc=%d)" % (
            error_text, stackwalk.returncode))

    return to_return

  @cherrypy.expose
  def wait_for_status(self, **kwargs):
    """Waits for background artifacts to be downloaded from Google Storage.

    Args:
      archive_url: Google Storage URL for the build.

    Example URL:
      'http://myhost/wait_for_status?archive_url=gs://chromeos-image-archive/'
      'x86-generic/R17-1208.0.0-a1-b338'
    """
    archive_url = kwargs.get('archive_url')
    if not archive_url:
      raise DevServerError("Didn't specify the archive_url in request")

    downloader_instance = self._downloader_dict.get(archive_url)
    if downloader_instance:
      status = downloader_instance.GetStatusOfBackgroundDownloads()
      self._downloader_dict[archive_url] = None
      return status
    else:
      # We may have previously downloaded but removed the downloader instance
      # from the cache.
      if downloader.Downloader.BuildStaged(archive_url, updater.static_dir):
        logging.info('%s not found in downloader cache but previously staged.',
                     archive_url)
        return 'Success'
      else:
        raise DevServerError('No download for the given archive_url found.')

  @cherrypy.expose
  def latestbuild(self, **params):
    """Return a string representing the latest build for a given target.

    Args:
      target: The build target, typically a combination of the board and the
          type of build e.g. x86-mario-release.
      milestone: The milestone to filter builds on. E.g. R16. Optional, if not
          provided the latest RXX build will be returned.
    Returns:
      A string representation of the latest build if one exists, i.e.
          R19-1993.0.0-a1-b1480.
      An empty string if no latest could be found.
    """
    if not params:
      return _PrintDocStringAsHTML(self.latestbuild)

    if 'target' not in params:
      raise cherrypy.HTTPError('500 Internal Server Error',
                               'Error: target= is required!')
    try:
      return devserver_util.GetLatestBuildVersion(
          updater.static_dir, params['target'],
          milestone=params.get('milestone'))
    except devserver_util.DevServerUtilError as errmsg:
      raise cherrypy.HTTPError('500 Internal Server Error', str(errmsg))

  @cherrypy.expose
  def controlfiles(self, **params):
    """Return a control file or a list of all known control files.

    Example URL:
      To List all control files:
      http://dev-server/controlfiles?board=x86-alex-release&build=R18-1514.0.0
      To return the contents of a path:
      http://dev-server/controlfiles?board=x86-alex-release&build=R18-1514.0.0&control_path=client/sleeptest/control

    Args:
      build: The build i.e. x86-alex-release/R18-1514.0.0-a1-b1450.
      control_path: If you want the contents of a control file set this
        to the path. E.g. client/site_tests/sleeptest/control
        Optional, if not provided return a list of control files is returned.
    Returns:
      Contents of a control file if control_path is provided.
      A list of control files if no control_path is provided.
    """
    if not params:
      return _PrintDocStringAsHTML(self.controlfiles)

    if 'build' not in params:
      raise cherrypy.HTTPError('500 Internal Server Error',
                               'Error: build= is required!')

    if 'control_path' not in params:
      return devserver_util.GetControlFileList(updater.static_dir,
                                               params['build'])
    else:
      return devserver_util.GetControlFile(updater.static_dir, params['build'],
                                           params['control_path'])

  @cherrypy.expose
  def index(self):
    return 'Welcome to the Dev Server!'

  @cherrypy.expose
  def update(self, *args):
    label = '/'.join(args)
    body_length = int(cherrypy.request.headers.get('Content-Length', 0))
    data = cherrypy.request.rfile.read(body_length)
    return updater.HandleUpdatePing(data, label)


if __name__ == '__main__':
  usage = 'usage: %prog [options]'
  parser = optparse.OptionParser(usage=usage)
  parser.add_option('--archive_dir', dest='archive_dir',
                    help='serve archived builds only.')
  parser.add_option('--board', dest='board',
                    help='When pre-generating update, board for latest image.')
  parser.add_option('--clear_cache', action='store_true', default=False,
                    help='Clear out all cached udpates and exit')
  parser.add_option('--client_prefix', dest='client_prefix_deprecated',
                    help='No longer used.  It is still here so we don\'t break '
                    'scripts that used it.', default='')
  parser.add_option('--critical_update', dest='critical_update',
                    action='store_true', default=False,
                    help='Present update payload as critical')
  parser.add_option('--data_dir', dest='data_dir',
                    help='Writable directory where static lives',
                    default=os.path.dirname(os.path.abspath(sys.argv[0])))
  parser.add_option('--exit', action='store_true', default=False,
                    help='Don\'t start the server (still pregenerate or clear'
                         'cache).')
  parser.add_option('--factory_config', dest='factory_config',
                    help='Config file for serving images from factory floor.')
  parser.add_option('--for_vm', dest='vm', default=False, action='store_true',
                    help='Update is for a vm image.')
  parser.add_option('--image', dest='image',
                    help='Force update using this image.')
  parser.add_option('-p', '--pregenerate_update', action='store_true',
                    default=False, help='Pre-generate update payload.')
  parser.add_option('--payload', dest='payload',
                    help='Use update payload from specified directory.')
  parser.add_option('--port', default=8080,
                    help='Port for the dev server to use (default: 8080).')
  parser.add_option('--private_key', default=None,
                    help='Path to the private key in pem format.')
  parser.add_option('--production', action='store_true', default=False,
                    help='Have the devserver use production values.')
  parser.add_option('--proxy_port', default=None,
                    help='Port to have the client connect to (testing support)')
  parser.add_option('--src_image', default='',
                    help='Image on remote machine for generating delta update.')
  parser.add_option('-t', action='store_true', dest='test_image')
  parser.add_option('-u', '--urlbase', dest='urlbase',
                    help='base URL, other than devserver, for update images.')
  parser.add_option('--validate_factory_config', action="store_true",
                    dest='validate_factory_config',
                    help='Validate factory config file, then exit.')
  parser.add_option('-l', '--log-dir', default=None,
                    help=('Specify a directory for error and access logs. '
                          'Default None, i.e. no logging.'))
  (options, _) = parser.parse_args()

  devserver_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
  root_dir = os.path.realpath('%s/../..' % devserver_dir)
  serve_only = False

  static_dir = os.path.realpath('%s/static' % options.data_dir)
  os.system('mkdir -p %s' % static_dir)

  if options.log_dir and not os.path.isdir(options.log_dir):
    parser.error('%s is not a valid dir, provide a valid dir to --log-dir' %
                 options.log_dir)

  if options.archive_dir:
  # TODO(zbehan) Remove legacy support:
  #  archive_dir is the directory where static/archive will point.
  #  If this is an absolute path, all is fine. If someone calls this
  #  using a relative path, that is relative to src/platform/dev/.
  #  That use case is unmaintainable, but since applications use it
  #  with =./static, instead of a boolean flag, we'll make this relative
  #  to devserver_dir  to keep these unbroken. For now.
    archive_dir = options.archive_dir
    if not os.path.isabs(archive_dir):
      archive_dir = os.path.realpath(os.path.join(devserver_dir, archive_dir))
    _PrepareToServeUpdatesOnly(archive_dir, static_dir)
    static_dir = os.path.realpath(archive_dir)
    serve_only = True

  cache_dir = os.path.join(static_dir, 'cache')
  cherrypy.log('Using cache directory %s' % cache_dir, 'DEVSERVER')

  if os.path.exists(cache_dir):
    if options.clear_cache:
      # Clear the cache and exit on error.
      cmd = 'rm -rf %s/*' % cache_dir
      if os.system(cmd) != 0:
        cherrypy.log('Failed to clear the cache with %s' % cmd,
                     'DEVSERVER')
        sys.exit(1)

    else:
      # Clear all but the last N cached updates
      cmd = ('cd %s; ls -tr | head --lines=-%d | xargs rm -rf' %
             (cache_dir, CACHED_ENTRIES))
      if os.system(cmd) != 0:
        cherrypy.log('Failed to clean up old delta cache files with %s' % cmd,
                     'DEVSERVER')
        sys.exit(1)
  else:
    os.makedirs(cache_dir)

  if options.client_prefix_deprecated:
    cherrypy.log('The --client_prefix argument is DEPRECATED, '
                 'and is no longer needed.', 'DEVSERVER')

  cherrypy.log('Data dir is %s' % options.data_dir, 'DEVSERVER')
  cherrypy.log('Source root is %s' % root_dir, 'DEVSERVER')
  cherrypy.log('Serving from %s' % static_dir, 'DEVSERVER')

  updater = autoupdate.Autoupdate(
      root_dir=root_dir,
      static_dir=static_dir,
      serve_only=serve_only,
      urlbase=options.urlbase,
      test_image=options.test_image,
      factory_config_path=options.factory_config,
      forced_image=options.image,
      forced_payload=options.payload,
      port=options.port,
      proxy_port=options.proxy_port,
      src_image=options.src_image,
      vm=options.vm,
      board=options.board,
      copy_to_static_root=not options.exit,
      private_key=options.private_key,
      critical_update=options.critical_update,
  )

  # Sanity-check for use of validate_factory_config.
  if not options.factory_config and options.validate_factory_config:
    parser.error('You need a factory_config to validate.')

  if options.factory_config:
    updater.ImportFactoryConfigFile(options.factory_config,
                                     options.validate_factory_config)
    # We don't run the dev server with this option.
    if options.validate_factory_config:
      sys.exit(0)
  elif options.pregenerate_update:
    if not updater.PreGenerateUpdate():
      sys.exit(1)

  # If the command line requested after setup, it's time to do it.
  if not options.exit:
    cherrypy.quickstart(DevServerRoot(), config=_GetConfig(options))
