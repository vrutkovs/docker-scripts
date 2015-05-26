# -*- coding: utf-8 -*-

import sys
import json
import tempfile
import shutil
import os
import random
import hashlib
import datetime
import tarfile
import six

from docker_scripts.lib import common
from docker_scripts.errors import SquashError

if not six.PY3:
    import lib.xtarfile


class Chdir(object):

    """ Context manager for changing the current working directory """

    def __init__(self, newPath):
        self.newPath = os.path.expanduser(newPath)

    def __enter__(self):
        self.savedPath = os.getcwd()
        os.chdir(self.newPath)

    def __exit__(self, etype, value, traceback):
        os.chdir(self.savedPath)


class Squash(object):

    def __init__(self, log, image, docker=None, from_layer=None, tag=None, tmp_dir=None, output_path=None):
        self.log = log
        self.docker = docker
        self.image = image
        self.from_layer = from_layer
        self.tag = tag
        self.tmp_dir = tmp_dir
        self.output_path = output_path

        if not docker:
            self.docker = common.docker_client()

    def _read_layers(self, layers, image_id):
        """ Reads the JSON metadata for specified layer / image id """

        layer = self.docker.inspect_image(image_id)
        layers.append(layer['Id'])

        if 'Parent' in layer and layer['Parent']:
            self._read_layers(layers, layer['Parent'])

    def _save_image(self, image_id, tar_file):
        """ Saves the image as a tar archive under specified name """

        for x in [0, 1, 2]:
            self.log.info("Saving image %s to %s file..." %
                          (image_id, tar_file))
            self.log.debug("Try #%s..." % (x + 1))

            try:
                image = self.docker.get_image(image_id)

                with open(tar_file, 'wb') as f:
                    f.write(image.data)

                self.log.info("Image saved!")
                return True
            except Exception as e:
                self.log.exception(e)
                self.log.warn(
                    "An error occured while saving the %s image, retrying..." % image_id)

        self.log.error("Couldn't save %s image!" % image_id)

        return False

    def _unpack(self, tar_file, directory):
        """ Unpacks tar archive to selected directory """

        self.log.info("Unpacking %s tar file to %s directory" %
                      (tar_file, directory))

        with tarfile.open(tar_file, 'r') as tar:
            tar.extractall(path=directory)

        # Remove the tar file early to save some space
        self.log.debug("Removing exported tar (%s)..." % tar_file)
        os.remove(tar_file)

        self.log.info("Archive unpacked!")

    def _move_layers(self, layers, src, dest):
        """
        This moves all the layers that should be copied as-is.
        In other words - all layers that are not meant to be squashed will be
        moved from the old image to the new image untouched.
        """
        for layer in layers:
            self.log.debug("Moving unmodified layer '%s'..." % layer)
            shutil.move(os.path.join(src, layer), dest)

    def _marker_files(self, tar):
        """
        Searches for marker files in the specified archive.

        Docker marker files are files taht have the .wh. prefix in the name.
        These files mark the corresponding file to be removed (hidden) when
        we start a container from the image.
        """
        marker_files = {}

        self.log.debug(
            "Searching for marker files in '%s' archive..." % tar.name)

        for member in tar.getmembers():
            if '.wh.' in member.name:
                self.log.debug("Found '%s' marker file" % member.name)
                marker_files[member.name] = member

        return marker_files

    def _files_in_layers(self, layers, directory):
        """
        Prepare a list of files in all layers
        """
        files = {}
        for layer in layers:
            self.log.debug("Generating list of files in layer '%s'..." % layer)
            tar_file = os.path.join(directory, layer, "layer.tar")
            with tarfile.open(tar_file, 'r', format=tarfile.PAX_FORMAT) as tar:
                files[layer] = tar.getnames()
            self.log.debug("Done, found %s files" % len(files[layer]))

        return files

    def _generate_target_json(self, old_image_id, new_image_id, squash_id, squashed_dir):
        json_file = os.path.join(squashed_dir, "json")
        squashed_tar = os.path.join(squashed_dir, "layer.tar")
        # Read the original metadata
        metadata = self.docker.inspect_image(old_image_id)

        # Update the fields
        metadata['Id'] = new_image_id
        metadata['Parent'] = squash_id
        metadata['Config']['Image'] = squash_id
        metadata['Created'] = datetime.datetime.utcnow().strftime(
            '%Y-%m-%dT%H:%M:%S.%fZ')
        metadata['Size'] = os.path.getsize(squashed_tar)

        # Remove unnecessary fields
        del metadata['ContainerConfig']
        del metadata['Container']
        del metadata['Config']['Hostname']

        with open(json_file, 'w') as f:
            json.dump(metadata, f)

    def _generate_repositories_json(self, repositories_file, image_id, name, tag):
        if not image_id:
            raise SquashError("Provided image id cannot be null")

        repos = {}
        repos[name] = {}
        repos[name][tag] = image_id

        data = json.dumps(repos)

        with open(repositories_file, 'w') as f:
            f.write(data)

    def _generate_image_id(self):
        while True:
            image_id = hashlib.sha256(
                str(random.getrandbits(128)).encode('utf8')).hexdigest()

            try:
                int(image_id[0:10])
            except ValueError:
                # All good!
                return image_id

    def _tar_image(self, output_path, directory):
        with tarfile.open(output_path, 'w') as tar:
            self.log.debug("Generating tar archive for the squashed image...")
            with Chdir(directory):
                tar.add(".")
            self.log.debug("Archive generated")

    def _load_image(self, directory):
        buf = six.BytesIO()

        with tarfile.open(mode='w', fileobj=buf) as tar:
            self.log.debug("Generating tar archive for the squashed image...")
            with Chdir(directory):
                tar.add(".")
            self.log.debug("Archive generated")

        self.log.info("Loading squashed image...")
        self.docker.load_image(buf.getvalue())
        self.log.info("Image loaded!")

        buf.close()

    def _layers_to_squash(self, layers, from_layer):
        """ Prepares a list of layer IDs that should be squashed """
        to_squash = []
        to_leave = []
        should_squash = True


        for l in reversed(layers):
            if l == from_layer:
                should_squash = False

            if should_squash:
                to_squash.append(l)
            else:
                to_leave.append(l)

        to_squash.reverse()
        to_leave.reverse()

        return to_squash, to_leave

    def _prepare_tmp_directory(self, provided_tmp_dir):
        """ Creates temporary directory that is used to work on layers """
        if provided_tmp_dir:
            if os.path.exists(provided_tmp_dir):
                raise SquashError(
                    "The '%s' directory already exists, please remove it before you proceed" % provided_tmp_dir)
            os.makedirs(provided_tmp_dir)
            return provided_tmp_dir
        else:
            return tempfile.mkdtemp(prefix="docker-squash-")

    def _parse_image_name(self, image):
        if ':' in image and not '/' in image.split(':')[-1]:
            image_tag = image.split(':')[-1]
            image_name = image[:-(len(image_tag) + 1)]
        else:
            image_tag = "latest"
            image_name = image

        return (image_name, image_tag)

    def _file_should_be_skipped(self, file_name, skipped_paths):
        for file_path in skipped_paths:
            if file_name == file_path or file_name.startswith(file_path + "/"):
                return True

        return False

    def _squash_layers(self, layers_to_squash, layers_to_move, squashed_tar_file, old_image_dir):
        self.log.info("Starting squashing...")

        # Reverse the layers to squash - we begin with the newest one
        # to make the tar lighter
        layers_to_squash.reverse()
        # Find all files in layers that we don't squash
        files_in_layers = self._files_in_layers(layers_to_move, old_image_dir)

        with tarfile.open(squashed_tar_file, 'w', format=tarfile.PAX_FORMAT) as squashed_tar:
            to_skip = []

            for layer_id in layers_to_squash:
                layer_tar_file = os.path.join(
                    old_image_dir, layer_id, "layer.tar")

                self.log.info("Squashing layer '%s'..." % layer_id)

                # Open the exiting layer to squash
                with tarfile.open(layer_tar_file, 'r', format=tarfile.PAX_FORMAT) as layer_tar:
                    # Find all marker files for all layers
                    markers = self._marker_files(layer_tar)
                    squashed_files = squashed_tar.getnames()

                    # Iterate over the marker files found for this particular
                    # layer
                    for marker_name, marker in six.iteritems(markers):
                        actual_file = marker_name.replace('.wh.', '')
                        if self._file_should_be_skipped(actual_file, squashed_files):
                            # Add all files (marker and actual) to skipped files
                            to_skip.append(marker_name)
                            to_skip.append(actual_file)

                    # Copy all the files to the new tar
                    for member in layer_tar.getmembers():
                        # Skip files that are marked to be skipped
                        if self._file_should_be_skipped(member.name, to_skip):
                            self.log.debug(
                                "Skipping '%s' file because it's on the list to skip files" % member.name)
                            continue

                        # List of filenames in the squashed archive
                        squashed_files = squashed_tar.getnames()

                        # Check if file is already added to the archive
                        if member.name in squashed_files:
                            # File already exist in the squashed archive, skip it because
                            # file want to add is older than the one already in the archive.
                            # This is true because we do reverse squashing - from
                            # newer to older layer
                            self.log.debug(
                                "Skipping '%s' file because it's older than file already added to the archive" % member.name)
                            continue

                        if member.issym():
                            # Special case: symlinks
                            squashed_tar.addfile(member)
                        else:
                            # Finally add the file to archive
                            squashed_tar.addfile(
                                member, layer_tar.extractfile(member))


        self.log.info("Squashing finished!")

    def run(self):

        self.log.info("Squashing image '%s'..." % self.image)

        # The image id or name of the image to be squashed
        try:
            old_image_id = self.docker.inspect_image(self.image)['Id']
        except SquashError:
            self.log.error(
                "Could not get the image ID to squash, please check provided 'image' argument: %s" % self.image)
            sys.exit(1)

        if self.tag:
            image_name, image_tag = self._parse_image_name(self.tag)
        else:
            image_name, image_tag = self._parse_image_name(self.image)

        old_layers = []

        # Read all layers in the image
        self._read_layers(old_layers, old_image_id)

        old_layers.reverse()

        # The id or name of the layer/image that the squashing should begin from
        # This layer WILL NOT be squashed, but all next layers will
        if self.from_layer:
            from_layer = self.from_layer
        else:
            from_layer = old_layers[0]

        try:
            squash_id = self.docker.inspect_image(from_layer)['Id']
        except SquashError:
            self.log.error(
                "Could not get the layer ID to squash, please check provided 'layer' argument: %s" % from_layer)
            sys.exit(1)

        self.log.info("Old image has %s layers", len(old_layers))
        self.log.debug("Old layers: %s", old_layers)

        if not squash_id in old_layers:
            self.log.error("Couldn't find the provided layer (%s) in the %s image" % (
                self.from_layer, self.image))
            sys.exit(1)

        # Find the layers to squash and to move
        layers_to_squash, layers_to_move = self._layers_to_squash(old_layers, squash_id)

        self.log.info("Attempting to squash from layer %s...", squash_id)
        self.log.info("Checking if squashing is necessary...")

        if len(layers_to_squash) <= 1:
            self.log.warning(
                "%s layer(s) in this image marked to squash, no squashing is required, exiting" % len(layers_to_squash))
            sys.exit(0)

        self.log.info("We have %s layers to squash", len(layers_to_squash))
        self.log.debug("Layers to squash: %s", layers_to_squash)

        # Prepare temporary directory where all the work will be executed
        try:
            tmp_dir = self._prepare_tmp_directory(self.tmp_dir)
        except:
            self.log.error("Preparing temporary directory failed, aborting")
            sys.exit(1)

        # Location of the tar with the old image
        old_image_tar = os.path.join(tmp_dir, "image.tar")

        # Save the image in tar format in the tepmorary directory
        if not self._save_image(old_image_id, old_image_tar):
            sys.exit(1)

        # Directory where the old layers will be unpacked
        old_image_dir = os.path.join(tmp_dir, "old")
        os.makedirs(old_image_dir)

        # Unpack the image
        self._unpack(old_image_tar, old_image_dir)

        # Directory where the new layers will be unpacked in prepareation to
        # import it to Docker
        new_image_dir = os.path.join(tmp_dir, "new")
        os.makedirs(new_image_dir)

        # Generate a new image id for the squashed layer
        new_image_id = self._generate_image_id()

        self.log.info(
            "New layer ID for squashed content will be: %s" % new_image_id)

        # Prepare a directory for squashed layer content
        squashed_dir = os.path.join(new_image_dir, new_image_id)
        os.makedirs(squashed_dir)

        # Location of the tar archive with the squashed layers
        squashed_tar = os.path.join(squashed_dir, "layer.tar")

        # Append all the layers on each other
        self._squash_layers(layers_to_squash, layers_to_move, squashed_tar, old_image_dir)

        # Move all the layers that should be untouched
        self._move_layers(
            layers_to_move, old_image_dir, new_image_dir)

        # Generate the metadata JSON based on the original one
        self._generate_target_json(
            old_image_id, new_image_id, squash_id, squashed_dir)

        # Generate the metadata JSON with information about the images
        self._generate_repositories_json(
            os.path.join(new_image_dir, "repositories"), new_image_id, image_name, image_tag)

        self.log.info("Squashed image name: %s:%s" % (image_name, image_tag))

        if self.output_path:
            # Move the tar archive to the specified path and exit
            # without loading into Docker
            self._tar_image(self.output_path, new_image_dir)
            self.log.info("Image available at '%s'" % self.output_path)
        else:
            # Load image into Docker
            self._load_image(new_image_dir)
            self.log.info("Image registered in Docker daemon")

        # Cleanup the temporary directory
        shutil.rmtree(tmp_dir)

        self.log.info("Done")

        return new_image_id
