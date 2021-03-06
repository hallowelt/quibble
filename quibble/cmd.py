#!/usr/bin/env python3
#
# Copyright 2017-2018, Antoine "hashar" Musso
# Copyright 2017, Tyler Cipriani
# Copyright 2017-2018, Wikimedia Foundation Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.

import argparse
from contextlib import ExitStack
import json
import logging
import os
import pkg_resources
from shutil import copyfile
import subprocess
import sys
import tempfile

import quibble
import quibble.mediawiki.maintenance
import quibble.backend
import quibble.test
import quibble.zuul


class QuibbleCmd(object):

    log = logging.getLogger('quibble.cmd')
    stages = ['phpunit', 'npm-test', 'composer-test', 'qunit', 'selenium']
    dump_dir = None
    db_dir = None

    def __init__(self):
        self.dependencies = []
        # Hold backend objects so they do not get garbage collected until end
        # of script.
        self.backends = {}
        self.default_git_cache = ('/srv/git' if quibble.is_in_docker()
                                  else 'ref')
        self.default_workspace = ('/workspace' if quibble.is_in_docker()
                                  else os.getcwd())

    def parse_arguments(self, args=sys.argv[1:]):
        return self.get_arg_parser().parse_args(args)

    def get_arg_parser(self):
        """
        Parse arguments
        """
        parser = argparse.ArgumentParser(
            description='Quibble: the MediaWiki test runner',
            prog='quibble',
            )
        parser.add_argument(
            '--packages-source',
            choices=['composer', 'vendor'],
            default='vendor',
            help='Source to install PHP dependencies from. Default: vendor')
        parser.add_argument(
            '--skip-zuul',
            action='store_true',
            help='Do not clone/checkout in workspace')
        parser.add_argument(
            '--skip-deps',
            action='store_true',
            help='Do not run composer/npm')
        parser.add_argument(
            '--db',
            choices=['sqlite', 'mysql', 'postgres'],
            default='mysql',
            help='Database backend to use. Default: mysql')
        parser.add_argument(
            '--db-dir',
            default=None,
            help=(
                'Base directory holding database files. A sub directory '
                'prefixed with "quibble-" will be created and deleted '
                'on completion. '
                'If set and relative, relatively to workspace. '
                'Default: %s' % tempfile.gettempdir()
            )
        )
        parser.add_argument(
            '--dump-db-postrun',
            action='store_true',
            help='Dump the db before shutting down the server (mysql only)')
        parser.add_argument(
            '--git-cache',
            default=self.default_git_cache,
            help='Path to bare git repositories to speed up git clone'
                 'operation. Passed to zuul-cloner as --cache-dir. '
                 'In Docker: "/srv/git", else "ref"')
        parser.add_argument(
            '--branch',
            default=None,
            help=('Branch to checkout instead of Zuul selected branch, '
                  'for example to specify an alternate branch to test '
                  'client library compatibility.')
            )
        parser.add_argument(
            '--project-branch', nargs=1, action='append',
            default=[],
            metavar='PROJECT=BRANCH',
            help=('project-specific branch to checkout which takes precedence '
                  'over --branch if it is provided; may be specified multiple '
                  'times.')
            )
        parser.add_argument(
            '--workspace',
            default=self.default_workspace,
            help='Base path to work from. In Docker: "/workspace", '
                 'else current working directory'
            )
        parser.add_argument(
            '--log-dir',
            default=os.path.join(self.default_workspace, 'log'),
            help='Where logs and artifacts will be written to. '
            'Default: "log" relatively to workspace'
            )
        parser.add_argument(
            'projects', default=[], nargs='*',
            help='MediaWiki extensions and skins to clone. Always clone '
                 'mediawiki/core and mediawiki/skins/Vector. '
                 'If $ZUUL_PROJECT is set, it will be cloned as well.'
            )

        stages = ', '.join(self.stages)

        stages_args = parser.add_argument_group('stages', description=(
            'Quibble runs all test commands (stages) by default. '
            'Use the --run or --skip options to further refine which commands '
            'will be run. '
            'Available stages are: %s' % stages))
        stages_args.add_argument(
            '--run', default=['all'], nargs='*',
            help='Tests to run (default: all).'
        )
        stages_args.add_argument(
            '--skip', default=[], nargs='*',
            help='Stages to skip (default: none). '
                 'Set to "all" to skip all stages.'
        )
        stages_args.add_argument(
            '--commands', default=[], nargs='*', metavar='command',
            help=(
                'Run given commands instead of built-in stages. '
                'Each command is executed relatively to '
                'MediaWiki installation path.'))

        parser.add_argument(
            '--phpunit-testsuite', default=None, metavar='pattern',
            help='PHPUnit: filter which testsuite to run')

        return parser

    def copylog(self, src, dest):
        dest = os.path.join(self.log_dir, dest)
        self.log.info('Copying %s to %s' % (src, dest))
        copyfile(src, dest)

    def setup_environment(self):
        """
        Set and get needed environment variables.
        """
        if 'EXECUTOR_NUMBER' not in os.environ:
            os.environ['EXECUTOR_NUMBER'] = '1'

        if quibble.is_in_docker() or 'WORKSPACE' not in os.environ:
            # Override WORKSPACE in Docker, we really want /workspace or
            # whatever was given from the command line.
            # Else set it, since some code might rely on it being set to detect
            # whether they are under CI.
            os.environ['WORKSPACE'] = self.workspace

        os.environ['MW_INSTALL_PATH'] = self.mw_install_path
        os.environ['MW_LOG_DIR'] = self.log_dir
        os.environ['LOG_DIR'] = self.log_dir
        os.environ['TMPDIR'] = tempfile.gettempdir()

    def set_repos_to_clone(self, projects=[], clone_vendor=False):
        """
        Find repos to clone basedon passed arguments and environment
        """
        # mediawiki/core should be first else git clone will fail because the
        # destination directory already exists.
        self.dependencies.insert(0, 'mediawiki/core')
        self.dependencies.append('mediawiki/skins/Vector')
        if clone_vendor:
            self.log.info('Adding mediawiki/vendor')
            self.dependencies.append('mediawiki/vendor')

        if 'ZUUL_PROJECT' in os.environ:
            zuul_project = os.environ.get('ZUUL_PROJECT')
            if zuul_project not in self.dependencies:
                self.dependencies.append(zuul_project)

        if 'SKIN_DEPENDENCIES' in os.environ:
            self.dependencies.extend(
                os.environ.get('SKIN_DEPENDENCIES').split('\\n'))

        if 'EXT_DEPENDENCIES' in os.environ:
            self.dependencies.extend(
                os.environ.get('EXT_DEPENDENCIES').split('\\n'))

        self.dependencies.extend(projects)

        self.log.info('Projects: %s'
                      % ', '.join(self.dependencies))

        return self.dependencies

    def clone(self, projects):
        quibble.zuul.clone(
            projects,
            branch=self.args.branch,
            project_branch=self.args.project_branch,
            workspace=os.path.join(self.workspace, 'src'),
            cache_dir=self.args.git_cache)

    def ext_skin_submodule_update(self):
        self.log.info('Updating git submodules of extensions and skins')
        # From JJB macro ext-skins-submodules-update
        # jjb/mediawiki-extensions.yaml
        subprocess.check_call([
            # Do not add ., or that will process mediawiki/core submodules in
            # wmf branches which is a mess.
            'find', 'extensions', 'skins',
            '-maxdepth', '2',
            '-name', '.gitmodules',
            '-print',
            '-execdir', 'bash', '-xe', '-c',
            '\n'.join([
                 'git submodule foreach git clean -xdff -q',
                 'git submodule update --init --recursive',
                 'git submodule status',
                 ]),
            ';',  # end of -execdir
             ], cwd=self.mw_install_path)

    # Used to be bin/mw-create-composer-local.py
    def create_composer_local(self):
        self.log.info('composer.local.json for merge plugin')
        extensions = [ext.strip()[len('mediawiki/'):] + '/composer.json'
                      for ext in self.dependencies
                      if ext.strip().startswith('mediawiki/extensions/')]
        out = {
            'extra': {
                'merge-plugin': {'include': extensions}
                }
            }
        composer_local = os.path.join(self.mw_install_path,
                                      'composer.local.json')
        with open(composer_local, 'w') as f:
            json.dump(out, f)
        self.log.info('Created composer.local.json')

    def mw_install(self):
        dbclass = quibble.backend.getDBClass(engine=self.args.db)
        db = dbclass(base_dir=self.db_dir, dump_dir=self.dump_dir)
        self.backends['db'] = db  # hold a reference to prevent gc
        db.start()

        install_args = [
            '--scriptpath=',
            '--dbtype=%s' % self.args.db,
            '--dbname=%s' % db.dbname,
        ]
        if self.args.db == 'sqlite':
            install_args.extend([
                '--dbpath=%s' % db.rootdir,
            ])
        elif self.args.db in ('mysql', 'postgres'):
            install_args.extend([
                '--dbuser=%s' % db.user,
                '--dbpass=%s' % db.password,
                '--dbserver=%s' % db.dbserver,
            ])
        else:
            raise Exception('Unsupported database: %s' % self.args.db)

        quibble.mediawiki.maintenance.install(
            args=install_args,
            mwdir=self.mw_install_path
        )

        localsettings = os.path.join(self.mw_install_path, 'LocalSettings.php')
        # Prepend our custom configuration snippets
        with open(localsettings, 'r+') as lf:
            extra_conf = subprocess.check_output([
                'php',
                pkg_resources.resource_filename(
                    __name__, 'mediawiki.d/_join.php')
                ])
            installed_conf = lf.read()
            lf.seek(0, 0)
            lf.write(extra_conf.decode() + installed_conf)
        subprocess.check_call(['php', '-l', localsettings])
        self.copylog(localsettings, 'LocalSettings.php')

        update_args = []
        if self.args.packages_source == 'vendor':
            # When trying to update a library in mediawiki/core and
            # mediawiki/vendor, a circular dependency is produced as both
            # patches depend upon each other.
            #
            # All non-mediawiki/vendor jobs will skip checking for matching
            # versions and continue "at their own risk". mediawiki/vendor will
            # still check versions to make sure it stays in sync with MediaWiki
            # core.
            #
            # T88211
            self.log.info('mediawiki/vendor used. '
                          'Skipping external dependencies')
            update_args.append('--skip-external-dependencies')

        quibble.mediawiki.maintenance.update(
            args=update_args,
            mwdir=self.mw_install_path
        )
        quibble.mediawiki.maintenance.rebuildLocalisationCache(
            lang=['en'], mwdir=self.mw_install_path)

    def fetch_composer_dev(self):
        mw_composer_json = os.path.join(self.mw_install_path, 'composer.json')
        vendor_dir = os.path.join(self.mw_install_path, 'vendor')
        with open(mw_composer_json, 'r') as f:
            composer = json.load(f)

        reqs = ['='.join([dependency, version])
                for dependency, version in composer['require-dev'].items()]

        self.log.debug('composer require %s' % ' '.join(reqs))
        composer_require = ['composer', 'require', '--dev', '--ansi',
                            '--no-progress', '--prefer-dist', '-v']
        composer_require.extend(reqs)

        subprocess.check_call(composer_require, cwd=vendor_dir)

        if self.args.packages_source == 'vendor':
            # Point composer-merge-plugin to mediawiki/core.
            # That let us easily merge autoload-dev section and thus complete
            # the autoloader.
            # T158674
            subprocess.check_call([
                'composer', 'config',
                'extra.merge-plugin.include', mw_composer_json],
                cwd=vendor_dir)

        # FIXME integration/composer used to be outdated and broke the
        # autoloader. Since composer 1.0.0-alpha11 the following might not
        # be needed anymore.
        subprocess.check_call([
            'composer', 'dump-autoload', '--optimize'],
            cwd=vendor_dir)

        self.copylog(mw_composer_json, 'composer.core.json.txt')
        self.copylog(os.path.join(vendor_dir, 'composer.json'),
                     'composer.vendor.json.txt')
        self.copylog(os.path.join(vendor_dir, 'composer/autoload_files.php'),
                     'composer.autoload_files.php.txt')

    def isCoreOrVendor(self, project):
        return project == 'mediawiki/core' or project == 'mediawiki/vendor'

    def isExtOrSkin(self, project):
        return project.startswith(
            ('mediawiki/extensions/', 'mediawiki/skins/')
        )

    def should_run(self, stage):
        if self.args.commands:
            return False
        if 'all' in self.args.skip:
            return False
        if stage in self.args.skip:
            return False
        if 'all' in self.args.run:
            return True
        return stage in self.args.run

    def execute(self):
        logging.basicConfig(level=logging.INFO)
        logging.getLogger('quibble').setLevel(logging.DEBUG)
        quibble.colored_logging()

        self.args = self.parse_arguments()

        self.workspace = self.args.workspace
        self.mw_install_path = os.path.join(self.workspace, 'src')
        self.log_dir = os.path.join(self.workspace, self.args.log_dir)
        if self.args.db_dir is not None:
            self.db_dir = os.path.join(self.workspace, self.args.db_dir)

        os.makedirs(self.log_dir, exist_ok=True)

        if self.args.dump_db_postrun:
            self.dump_dir = self.log_dir

        self.log.debug('Running stages: '
                       + ', '.join(stage for stage in self.stages
                                   if self.should_run(stage)))

        self.setup_environment()

        zuul_project = os.environ.get('ZUUL_PROJECT', None)
        if zuul_project is None:
            self.log.warning('ZUUL_PROJECT not set. Assuming mediawiki/core')
            zuul_project = 'mediawiki/core'
        else:
            self.log.debug("ZUUL_PROJECT=%s" % zuul_project)

        projects_to_clone = self.set_repos_to_clone(
            projects=self.args.projects,
            clone_vendor=(self.args.packages_source == 'vendor'))

        if not self.args.skip_zuul:
            self.clone(projects_to_clone)
            self.ext_skin_submodule_update()

        if self.isExtOrSkin(zuul_project):
            run_composer = self.should_run('composer-test')
            run_npm = self.should_run('npm-test')
            if run_composer or run_npm:
                project_dir = os.path.join(
                    self.mw_install_path,
                    quibble.zuul.repo_dir(os.environ['ZUUL_PROJECT']))

                quibble.test.run_extskin(directory=project_dir,
                                         composer=run_composer, npm=run_npm)

                self.log.info('%s: git clean -xqdf' % project_dir)
                subprocess.check_call(['git', 'clean', '-xqdf'],
                                      cwd=project_dir)

        if not self.args.skip_deps and self.args.packages_source == 'composer':
            self.create_composer_local()
            self.log.info('Running "composer update for mediawiki/core')
            cmd = ['composer', 'update',
                   '--ansi', '--no-progress', '--prefer-dist',
                   '--profile', '-v',
                   ]
            subprocess.check_call(cmd, cwd=self.mw_install_path)

        self.mw_install()

        if not self.args.skip_deps:
            if self.args.packages_source == 'vendor':
                self.log.info('vendor.git used. '
                              'Requiring composer dev dependencies')
                self.fetch_composer_dev()

            subprocess.check_call(['npm', 'prune'], cwd=self.mw_install_path)
            subprocess.check_call(['npm', 'install'], cwd=self.mw_install_path)

        phpunit_testsuite = None
        if self.args.phpunit_testsuite:
            phpunit_testsuite = self.args.phpunit_testsuite
        elif zuul_project.startswith('mediawiki/extensions/'):
            phpunit_testsuite = 'extensions'
        elif zuul_project.startswith('mediawiki/skins/'):
            phpunit_testsuite = 'skins'

        if self.should_run('phpunit'):
            self.log.info("PHPUnit%swithout Database group" % (
                ' %s suite ' % (phpunit_testsuite or ' ')))
            # XXX might want to run the triggered extension first then the
            # other tests.
            # XXX some mediawiki/core smoke PHPunit tests should probably
            # be run as well.
            junit_dbless_file = os.path.join(
                self.log_dir, 'junit-dbless.xml')
            quibble.test.run_phpunit_databaseless(
                mwdir=self.mw_install_path,
                testsuite=phpunit_testsuite,
                junit_file=junit_dbless_file)

        if zuul_project == 'mediawiki/core':
            quibble.test.run_core(
                self.mw_install_path,
                composer=self.should_run('composer-test'),
                npm=self.should_run('npm-test')
            )

        http_port = 9412
        if self.should_run('qunit') or self.should_run('selenium'):
            with quibble.backend.DevWebServer(
                    mwdir=self.mw_install_path,
                    port=http_port):
                if self.should_run('qunit'):
                    quibble.test.run_qunit(self.mw_install_path,
                                           port=http_port)

                # Webdriver.io Selenium tests available since 1.29
                if self.should_run('selenium') and \
                        os.path.exists(os.path.join(
                            self.mw_install_path, 'tests/selenium')):
                    with ExitStack() as stack:
                        display = os.environ.get('DISPLAY', None)
                        if not display:
                            display = ':94'  # XXX racy when run concurrently!
                            self.log.info("No DISPLAY, using Xvfb.")
                            stack.enter_context(
                                quibble.backend.Xvfb(display=display))

                        with quibble.backend.ChromeWebDriver(display=display):
                            quibble.test.run_webdriver(
                                mwdir=self.mw_install_path,
                                port=http_port,
                                display=display)

        if self.should_run('phpunit'):
            self.log.info("PHPUnit%sDatabase group" % (
                ' %s suite ' % (phpunit_testsuite or ' ')))
            junit_db_file = os.path.join(
                self.log_dir, 'junit-db.xml')
            quibble.test.run_phpunit_database(
                mwdir=self.mw_install_path,
                testsuite=phpunit_testsuite,
                junit_file=junit_db_file)

        if self.args.commands:
            self.log.info('User commands')
            with quibble.backend.DevWebServer(
                    mwdir=self.mw_install_path,
                    port=http_port):
                quibble.test.commands(
                    self.args.commands,
                    cwd=self.mw_install_path)


def get_arg_parser():
    """
    Build an argparser with sane default values.

    Intended for documentation generation with sphinx-argparse.
    """
    cmd = QuibbleCmd()
    cmd.default_git_cache = 'ref'
    cmd.default_workspace = '.'
    cmd.default_logdir = './log'

    return cmd.get_arg_parser()


def main():
    cmd = QuibbleCmd()
    cmd.execute()


if __name__ == '__main__':
    main()
