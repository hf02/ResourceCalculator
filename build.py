import argparse
import gzip
import htmlmin
import json
import math
import os
import re
import shutil
import subprocess
import time
import yaml
from collections import OrderedDict
from jinja2 import Environment, FileSystemLoader
from PIL import Image
from typing import Tuple, List

from pylib.json_data_compressor import mini_js_data
from pylib.uglifyjs import uglify_copyfile, uglify_js_string
from pylib.webminify import minify_css_blocks

# CLI Argument Flags
FLAG_skip_js_lint = False
FLAG_skip_index = False
FLAG_skip_gz_compression = False
FLAG_skip_uglify_js = False
FLAG_skip_image_compress = False
FLAG_force_image = False


################################################################################
# ordered_load
#
# This function will load in the yaml recipe file but maintain the order of the
# items. This allows us to simplify the file definition making it easier for
# humans to use while also allowing us to set the order of the items for easy
# grouping
#
# https://stackoverflow.com/questions/5121931/in-python-how-can-you-load-yaml-mappings-as-ordereddicts
################################################################################
def ordered_load(stream, Loader=yaml.Loader, object_pairs_hook=OrderedDict):
    class OrderedLoader(Loader):
        pass

    def construct_mapping(loader, node):
        loader.flatten_mapping(node)
        return object_pairs_hook(loader.construct_pairs(node))
    OrderedLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_mapping)
    return yaml.load(stream, OrderedLoader)


################################################################################
# create_packed_image
#
# This function will take all the files within the resource_lists/[list]/items
# and create a single packed image of them. Then return the coordinates so that
# css can be written to load all of the images from the same file instead of
# making a large number of get requests for the file
################################################################################
def create_packed_image(calculator_name):

    resource_image_folder = os.path.join("resource_lists", calculator_name, "items")

    image_coordinates = {}

    standard_width = None
    standard_height = None
    standard_image_reference = None

    images: List[Tuple[str, str]] = []

    for file in os.listdir(resource_image_folder):
        images.append((
            os.path.os.path.splitext(file)[0],
            os.path.join(resource_image_folder, file)
        ))

    # Open first image to get a standard
    first_image = Image.open(images[0][1])
    standard_width, standard_height = first_image.size
    standard_image_reference = images[0][1]

    # Sort the images, this is probably not necessary but will allow for
    # differences between files to be noticed with less noise of random shifting of squares
    images.sort(key=lambda x: x[0])

    # Use our special math function to determine what the number of columns
    # should be for the final packed image.
    # Programmers note: This was a lot of fun to figure out and derived strangely
    columns = math.ceil(math.sqrt(standard_height * len(images) / standard_width))
    result_width = standard_width * columns
    result_height = standard_height * math.ceil((len(images) / columns))

    # Determine where each image should go
    for index, (name, image) in enumerate(images):
        x_coordinate = (index % columns) * standard_width
        y_coordinate = math.floor(index / columns) * standard_height
        image_coordinates[name] = (x_coordinate, y_coordinate)

    # Create a new output file and write all the images to spots in the file
    calculator_folder = os.path.join("output", calculator_name)
    output_image_path = os.path.join(calculator_folder, calculator_name + ".png")

    should_create_image = True

    if os.path.exists(output_image_path):
        newest_file = max(
            os.path.getctime("build.py"),  # Check generator code modification
            get_newest_modified_time("pylib"),  # Check generator code modification
            get_newest_modified_time(resource_image_folder),  # Check source image modification
        )
        should_create_image = newest_file > os.path.getctime(output_image_path)

    # Create or skip creation of the packed image
    if should_create_image or FLAG_force_image:

        # Create the new packed image file and all the coordinates of the images
        result = Image.new('RGBA', (result_width, result_height))
        for image_name, image_path in images:
            image = Image.open(image_path)
            width, height = image.size

            if (standard_width != width or standard_height != height):
                print("ERROR: All resource list item images for a single calculator must be the same size")
                print("       " + image_path + " and " + standard_image_reference + " are not the same size")

            x_coordinate, y_coordinate = image_coordinates[image_name]
            result.paste(im=image, box=(x_coordinate, y_coordinate))
        result.save(output_image_path)

        # Attempt to compress the image but do not exit on failure
        if not FLAG_skip_image_compress:
            try:
                subprocess.run(["pngquant", "--force", "--ext", ".png", "256", "--nofs", output_image_path])
            except OSError as e:
                print("WARNING: PNG Compression Failed")
                print("        ", e)
    else:
        print("  Skipping image generation because no source images have changed since last generated")

    return (standard_width, standard_height, image_coordinates)


################################################################################
# lint_javascript
#
# This function will attempt to call a javascript linter on the calculator.js
# file. If the linting process fails then a warning will be thrown but the
# process will not be ended.
################################################################################
def lint_javascript():
    try:
        subprocess.run(["./node_modules/.bin/eslint", "core/calculator.js"])
    except OSError as e:
        print("WARNING: Javascript linting failed")
        print("        ", e)


################################################################################
# lint_recipe
#
# This function takes in the name of the calculator, an item name, and the list
# of recipes to ensure that the given item's recipes all follow a set of
# patterns in order to make sure that all recipe lists are uniform in style
# and contents. In addition it makes sure that all of the required elements of
# a recipe are present and that no additional unknown elements are present.
################################################################################
def lint_recipes(calculator_name, item_name, recipes):

    required_keys = ["output", "recipe_type", "requirements"]
    optional_keys = ["extra_data"]

    valid_keys = required_keys + optional_keys

    for i, recipe in enumerate(recipes):

        recipe_keys = [x for x in recipe]

        # Make sure all the required keys exist
        for required_key in required_keys:
            if required_key not in recipe_keys:
                print(calculator_name.upper() + ":", "\"" + required_key + "\" is not in", item_name, "recipe", i)

        # Make sure that all the keys being used are valid keys
        for recipe_key in recipe_keys:
            if recipe_key not in valid_keys:
                print(calculator_name.upper() + ":", "\"" + recipe_key + "\" in", item_name, "recipe", i, "is not a valid key")

        # Validate the keys are in the right order to promote uniformity in the templates
        if (recipe_keys[0] != valid_keys[0]):
            # print(item_name, "recipe", i, "should have the first element of the hash be \"output\"")
            print(calculator_name.upper() + ":", "\"output\" should be the first key of", item_name, "recipe", i)
        if (recipe_keys[1] != valid_keys[1]):
            # print(item_name, "recipe", i, "should have the first element of the hash be \"output\"")
            print(calculator_name.upper() + ":", "\"recipe_type\" should be the second key of", item_name, "recipe", i)
        if (recipe_keys[2] != valid_keys[2]):
            # print(item_name, "recipe", i, "should have the first element of the hash be \"output\"")
            print(calculator_name.upper() + ":", "\"requirements\" should be the third key of", item_name, "recipe", i)

    # Check that every resource has a raw recipe
    raw_resource_count = 0
    for recipe in recipes:
        if (recipe['recipe_type'] == "Raw Resource"):
            if (recipe == OrderedDict([('output', 1), ('recipe_type', 'Raw Resource'), ('requirements', OrderedDict([(item_name, 0)]))])):
                raw_resource_count += 1
            else:
                print(calculator_name.upper() + ":", item_name, "has an invalid \"Raw Resource\"")

    # Lint that every resource has a raw resource and only one
    if raw_resource_count == 0:
        print(calculator_name.upper() + ":", item_name, "must have a \"Raw Resource\" which outputs 1 and has a requirement of 0 of itself")
    elif raw_resource_count > 1:
        print(calculator_name.upper() + ":", item_name, "must have only one \"Raw Resource\"")


def lint_resources(calculator_name, resources, recipe_types, stack_sizes):
    valid_keys = OrderedDict([
        ("custom_simplename", False),
        ("custom_stack_multipliers", False),
        ("recipes", True),
    ])

    for resource in resources:
        resource_keys = [x for x in resources[resource]]

        # Check that all required keys are in the resource
        for valid_key in valid_keys:
            if valid_keys[valid_key] and valid_key not in resource_keys:
                print(calculator_name.upper() + ":", "\"" + valid_key + "\" is not in", resource)

        # Check that all keys are required or optional
        for resource_key in resource_keys:
            if resource_key not in valid_keys.keys():
                print(calculator_name.upper() + ":", "\"" + resource_key + "\" in", resource, "is not a valid key")

        lint_recipes(calculator_name, resource, resources[resource]["recipes"])

        if "custom_stack_multipliers" in resources[resource]:
            lint_custom_stack_multipliers(calculator_name, resource, resources[resource]["custom_stack_multipliers"], stack_sizes)

    ensure_valid_requirements(resources)
    ensure_valid_recipe_types(calculator_name, resources, recipe_types)
    ensure_unique_simple_names(calculator_name, resources)


def lint_custom_stack_multipliers(calculator_name, item_name, custom_stack_multipliers, stack_sizes):
    for stack_name in custom_stack_multipliers:
        custom_size = custom_stack_multipliers[stack_name]

        if stack_name not in stack_sizes:
            print(calculator_name.upper() + ":", "custom_stack_size \"" + stack_name + "\" for", item_name, "is not a valid stack size. (" + ", ".join([x for x in stack_sizes]) + ")")

        if custom_size < 1:
            print(calculator_name.upper() + ":", "custom_stack_size \"" + stack_name + "\" for", item_name, "cannot be less than 1.")


def ensure_unique_simple_names(calculator_name, resources):
    simple_names = {}

    for resource in resources:
        simple_name = get_simple_name(resource, resources)
        if simple_name not in simple_names:
            simple_names[simple_name] = []
        simple_names[simple_name].append(resource)

    for simple_name in simple_names:
        if len(simple_names[simple_name]) > 1:
            print(calculator_name.upper() + ":", ", ".join(simple_names[simple_name]), "all share the same simple name", simple_name)


################################################################################
# ensure_valid_requirements
#
# Make sure each recipe requirement is another existing item in the resource list
################################################################################
def ensure_valid_requirements(resources):
    for resource in resources:
        for recipe in resources[resource]["recipes"]:
            for requirement in recipe["requirements"]:
                if requirement not in resources:
                    print("ERROR: Invalid requirement for resource:", resource + ". \"" + requirement + "\" does not exist as a resource")
                elif recipe["requirements"][requirement] > 0:
                    print("ERROR: Invalid requirement for resource:", resource + ". \"" + requirement + "\" must be a negative number")


def ensure_valid_recipe_types(calculator_name, resources, recipe_types):
    found_recipe_types = []
    for resource in resources:
        for recipe in resources[resource]["recipes"]:
            recipe_type = recipe["recipe_type"]
            # add this to the list of found recipe types to later check to make sure all the recipe_types in the list are used
            if recipe_type not in found_recipe_types and recipe_type != "Raw Resource":
                found_recipe_types.append(recipe_type)
            # check if this recipe exists in the recipe type list
            if recipe_type not in recipe_types and recipe_type != "Raw Resource":
                print(calculator_name.upper() + ":", resource + " has an undefined resource_type" + ": \"" + recipe_type + "\"")

    for recipe_type in recipe_types:
        if recipe_type not in found_recipe_types:
            print(calculator_name.upper() + ":", "Unused recipe_type \"" + recipe_type + "\"")


################################################################################
# get_oldest_modified_time
#
# This function takes a directory and finds the oldest modification time of any
# file in that directory.
################################################################################
def get_oldest_modified_time(path):
    time_list = []
    for file in os.listdir(path):
        filepath = os.path.join(path, file)
        if (os.path.isdir(filepath)):
            time_list.append(get_oldest_modified_time(filepath))
        else:
            time_list.append(os.path.getctime(filepath))
    if len(time_list) == 0:
        return 0
    return min(time_list)


################################################################################
# get_newest_modified_time
#
# This function takes in a directory and finds the newest modification time of
# any file in that directory
################################################################################
def get_newest_modified_time(path):
    time_list = []
    for file in os.listdir(path):
        filepath = os.path.join(path, file)
        if (os.path.isdir(filepath)):
            time_list.append(get_newest_modified_time(filepath))
        else:
            time_list.append(os.path.getctime(filepath))
    return max(time_list)


def generate_content_width_css(image_width, yaml_data):
    content_width_css = ""
    media_padding = 40  # This give a slight padding from the edges, useful for avoiding intersection with scroll bars
    if "row_group_count" in yaml_data:
        row_group_count = yaml_data["row_group_count"]
    else:
        row_group_count = 1

    iteration = 1
    image_width_with_padding = image_width + 6  # TODO: This will need to be updated if custom styling is added to the calculator
    while iteration * row_group_count * image_width_with_padding < 3840:
        content_width = iteration * row_group_count * image_width_with_padding
        screen_max = (iteration + 1) * row_group_count * image_width_with_padding
        new_css = "@media only screen and (max-width: " + str(screen_max + media_padding - 1) + "px) and (min-width:" + str(content_width + media_padding) + "px) { .resource_content { width: " + str(content_width) + "px}  }"
        content_width_css += new_css
        iteration += 1
    # When the width is less then a single group we still want the list to be centered
    for i in range(1, row_group_count):
        content_width = i * image_width_with_padding
        screen_max = (i + 1) * image_width_with_padding
        new_css = "@media only screen and (max-width: " + str(screen_max + media_padding - 1) + "px) and (min-width:" + str(content_width + media_padding) + "px) { .resource_content { width: " + str(content_width) + "px}  }"
        content_width_css += new_css

    return content_width_css


def get_simple_name(resource, resources):
    if "custom_simplename" in resources[resource]:
        return resources[resource]["custom_simplename"]
    return re.sub(r'[^a-z0-9]', '', resource.lower())


################################################################################
# generate_resource_html_data
#
#
################################################################################
def generate_resource_html_data(resources):
    resources_html_data = []
    for resource in resources:
        resource_html_data = {}
        simple_name = get_simple_name(resource, resources)
        resource_html_data["aria_label"] = resource
        resource_html_data["simplename"] = simple_name
        resources_html_data.append(resource_html_data)
    return resources_html_data


################################################################################
# generate_resource_offset_classes
#
#
################################################################################
def generate_resource_offset_classes(resources, resource_image_coordinates):
    item_styles = {}
    for resource in resources:
        simple_name = get_simple_name(resource, resources)

        if simple_name in resource_image_coordinates:
            x_coordinate, y_coordinate = resource_image_coordinates[simple_name]
            item_styles[simple_name] = "background-position: " + str(-x_coordinate) + "px " + str(-y_coordinate) + "px;"
        else:
            item_styles[simple_name] = "background: #f0f; background-image: none;"
            print("WARNING:", simple_name, "has a recipe but no image and will appear purple in the calculator")

    return item_styles


def get_recipes_only(resources):
    return {resource: resources[resource]["recipes"] for resource in resources}


def merge_custom_multipliers(stack_sizes, resources):
    for resource in resources:
        if "custom_stack_multipliers" in resources[resource]:
            custom_sizes = resources[resource]["custom_stack_multipliers"]
            for custom_size_name in custom_sizes:
                custom_size = custom_sizes[custom_size_name]

                if "custom_multipliers" not in stack_sizes[custom_size_name]:
                    stack_sizes[custom_size_name]["custom_multipliers"] = OrderedDict()

                stack_sizes[custom_size_name]["custom_multipliers"][resource] = custom_size

    return stack_sizes


def touch_output_folder_files(calculator_folder: str, timestamp: int = 0) -> None:
    if timestamp == 0:
        timestamp = int(time.time())

    for file in os.listdir(calculator_folder):
        filepath = os.path.join(calculator_folder, file)
        if (os.path.isdir(filepath)):
            get_oldest_modified_time(filepath, timestamp)
        else:
            os.utime(filepath, (timestamp, timestamp))


################################################################################
# create_calculator_page
#
# This function takes in a the name of a calculator resource list and creates
# the html page and resource for it. If no files have been changed for the
# calculator since the last time it was created then the creation will be skipped
################################################################################
def create_calculator_page(
    calculator_name: str,
    force: bool = False,
    print_skip_text: bool = True
):
    calculator_folder = os.path.join("output", calculator_name)
    source_folder = os.path.join("resource_lists", calculator_name)
    if not os.path.exists(calculator_folder):
        os.makedirs(calculator_folder)
    elif not force:
        oldest_output = get_oldest_modified_time(calculator_folder)
        newest_resource = get_newest_modified_time(source_folder)
        newest_corelib = get_newest_modified_time("core")
        newest_build_script = os.path.getctime("build.py")
        newest_build_lib = get_newest_modified_time("pylib")
        if oldest_output > max(newest_resource, newest_corelib, newest_build_script, newest_build_lib):
            # Allow not printing the skip text for polling with the --watch flag
            if print_skip_text:
                print("Skipping", calculator_name, "Nothing has changed since the last build")
            return

    start_time = time.time()

    print("Generating", calculator_name, "into", calculator_folder)

    # Create a packed image of all the item images
    image_width, image_height, resource_image_coordinates = create_packed_image(calculator_name)

    # Load in the yaml resources file
    with open(os.path.join("resource_lists", calculator_name, "resources.yaml"), 'r', encoding="utf_8") as f:
        yaml_data = ordered_load(f, yaml.SafeLoader)

    resources = yaml_data["resources"]

    authors = yaml_data["authors"]

    recipe_types = yaml_data["recipe_types"]

    stack_sizes = None
    if "stack_sizes" in yaml_data:
        stack_sizes = yaml_data["stack_sizes"]

    default_stack_size = None
    if "default_stack_size" in yaml_data:
        default_stack_size = yaml_data["default_stack_size"]

    # run some sanity checks on the resources
    lint_resources(calculator_name, resources, recipe_types, stack_sizes)
    # TODO: Add linting for stack sizes here

    recipe_type_format_js = generate_recipe_type_format_js(calculator_name, recipe_types)
    if not FLAG_skip_uglify_js:
        recipe_type_format_js = uglify_js_string(recipe_type_format_js)

    recipe_js = mini_js_data(get_recipes_only(resources))

    html_resource_data = generate_resource_html_data(resources)

    item_styles = generate_resource_offset_classes(resources, resource_image_coordinates)

    # Generate some css to allow us to center the list
    content_width_css = generate_content_width_css(image_width, yaml_data)

    stack_sizes = merge_custom_multipliers(stack_sizes, resources)

    stack_sizes_json = json.dumps(stack_sizes)

    # Generate the calculator from a template
    env = Environment(loader=FileSystemLoader('core'))
    template = env.get_template("calculator.html")
    output_from_parsed_template = template.render(
        # A simplified list used for creating the item selector HTML
        resources=html_resource_data,
        # the javascript/json object used for calculations
        recipe_json=recipe_js,
        # The size and positions of the image
        item_width=image_width,
        item_height=image_height,
        item_styles=item_styles,
        # The name of the calculator
        resource_list=calculator_name,
        # Javascript formatting functions for recipe instructions # TODO this should be made into format strings to save space
        recipe_type_format_js=recipe_type_format_js,
        # The list of authors and emails to display in the authors sections
        authors=authors,
        # Additional CSS to center the list when resizing
        content_width_css=content_width_css,
        # Used to build the stack size selector UI
        stack_sizes=stack_sizes,
        default_stack_size=default_stack_size,
        # Used to do calculations to divide counts into stacks
        stack_sizes_json=stack_sizes_json)

    minified = htmlmin.minify(output_from_parsed_template, remove_comments=True, remove_empty_space=True)
    minified = minify_css_blocks(minified)

    with open(os.path.join(calculator_folder, "index.html"), "w", encoding="utf_8") as f:
        f.write(minified)

    # Sanity Check Warning, is there an image that does not have a recipe
    simple_resources = [x["simplename"] for x in html_resource_data]
    for simple_name in resource_image_coordinates:
        if simple_name not in simple_resources:
            print("WARNING:", simple_name, "has an image but no recipe and will not appear in the calculator")

    # Touch the created time of all the files in the output folder to prevent
    # re-triggering generation on outdated files that were intentionally skipped
    touch_output_folder_files(calculator_folder)

    end_time = time.time()
    print("  Generated in %.3f seconds" % (end_time - start_time))


# [{
#         "name":
#         "tokenized_inputs": []
#         "input_chunks": [
#             {
#                 "type":"text",
#                 "text":"Craft"
#             },
#             {
#                 "type":"output"
#             }
#             {
#                 "type":"all_other_inputs"
#             }
#             {
#                 "type":"tokenized_input",
#                 "name":"Fuel"
#             }
#         ]
#     }]
def generate_recipe_type_format_js(calculator_name, recipe_types):
    recipe_type_format_functions = []

    for recipe_type in recipe_types:
        recipe_type_format_string = recipe_types[recipe_type]

        all_chunks = re.split('({[^}]+})', recipe_type_format_string)

        tokenized_inputs = []
        input_chunks = []

        for chunk in all_chunks:
            if len(chunk) < 1:
                continue

            if chunk.startswith("{") and chunk.endswith("}"):
                chunk = chunk[1:-1]

                if chunk == "IN_ITEMS":
                    input_chunks.append({"type": "all_other_inputs"})
                elif chunk == "OUT_ITEM":
                    input_chunks.append({"type": "output"})
                elif chunk.startswith("ITEM"):
                    tokenized_item_name = chunk[5:]
                    input_chunks.append({"type": "tokenized_input", "name": tokenized_item_name})
                    tokenized_inputs.append(tokenized_item_name)
                    # TODO: some linting here can be done to make sure that all recipe_types that have this tokenized item have the item
                else:
                    print("UNKNOWN IDENTIFIER IN FORMAT STRING")  # TODO makethis error message better

            else:
                input_chunks.append({"type": "text", "text": chunk})

        format_function = {
            "name": recipe_type,
            "tokenized_inputs": json.dumps(tokenized_inputs),
            "input_chunks": input_chunks
        }

        recipe_type_format_functions.append(format_function)

    env = Environment(loader=FileSystemLoader('core'))
    template = env.get_template("_recipe_type_display_functions.js")

    return template.render(recipe_type_format_functions=recipe_type_format_functions)

    # return recipe_type_format_functions


################################################################################
# create_index_page
#
# This function creates an index page with all of the calculator links
################################################################################
def create_index_page(directories):
    for directory in directories:
        shutil.copyfile("resource_lists/" + directory + "/icon.png", "output/" + directory + "/icon.png")

    # Configure and begin the jinja2 template parsing
    env = Environment(loader=FileSystemLoader('core'))
    template = env.get_template("index.html")

    calculators = []
    for directory in directories:
        calculator_data = {
            "path": directory,
            "display_name": calculator_display_name(directory)
        }

        calculators.append(calculator_data)

    output_from_parsed_template = template.render(calculators=calculators)

    with open(os.path.join("output", "index.html"), "w", encoding="utf_8") as f:
        f.write(output_from_parsed_template)


################################################################################
# calculator_display_name
#
# Reads the resources yaml file and grabs the display name of that calculator
################################################################################
def calculator_display_name(calculator_name):
    with open(os.path.join("resource_lists", calculator_name, "resources.yaml"), 'r', encoding="utf_8") as f:
        yaml_data = ordered_load(f, yaml.SafeLoader)
    return yaml_data["index_page_display_name"]


################################################################################
# pre_compress_output_files
#
# Walks through the output directory and compresses any file with a .html, .css
# or .js extension with gz so that Apache can serve its compressed state
# automatically.
################################################################################
def pre_compress_output_files() -> None:
    textfile_extensions = [".html", ".css", ".js"]
    for (root, dirs, files) in os.walk("output"):
        for file in files:
            if ends_with_any(file, textfile_extensions):
                filepath = os.path.join(root, file)

                # Gzip Compression
                with open(filepath, 'rb') as infile, gzip.open(filepath + ".gz", 'wb') as outfile:
                    shutil.copyfileobj(infile, outfile)


################################################################################
# ends_with_any
#
# A helper function to check to see if a string ends with any element of a
# list of strings.
################################################################################
def ends_with_any(string: str, endings: List[str]) -> bool:
    for ending in endings:
        if string.endswith(ending):
            return True
    return False


################################################################################
# copy_common_resources
#
# This is a hacky function to copy over some files that should be accessible
# by the code
################################################################################
def _uglify_copyfile(in_file: str, out_file: str) -> None:
    if FLAG_skip_uglify_js:
        shutil.copyfile(in_file, out_file)
    else:
        uglify_copyfile(in_file, out_file)


def copy_common_resources():
    shutil.copyfile("core/calculator.css", "output/calculator.css")
    _uglify_copyfile("core/calculator.js", "output/calculator.js")
    shutil.copyfile("core/thirdparty/jquery-3.3.1.min.js", "output/jquery.js")
    shutil.copyfile("core/logo.png", "output/logo.png")
    shutil.copyfile("core/.htaccess", "output/.htaccess")
    shutil.copyfile("core/add_game.png", "output/add_game.png")
    shutil.copyfile("core/ads.txt", "output/ads.txt")


def main():
    parser = argparse.ArgumentParser(
        description='Compile resourcecalculator.com html pages.'
    )

    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--no-jslint', action='store_true')
    parser.add_argument('--no-uglify-js', action='store_true')
    parser.add_argument('--no-gz', action='store_true')
    parser.add_argument('--no-index', action='store_true')
    parser.add_argument('--no-image-compress', action='store_true')
    parser.add_argument('--force-html', action='store_true')
    parser.add_argument('--force-image', action='store_true')
    parser.add_argument('limit_files', nargs='*')

    global FLAG_skip_index
    global FLAG_skip_js_lint
    global FLAG_skip_gz_compression
    global FLAG_skip_uglify_js
    global FLAG_skip_image_compress
    global FLAG_force_image

    args = parser.parse_args()
    if (args.watch):
        pass

    if args.no_jslint:
        FLAG_skip_js_lint = True
    if args.no_uglify_js:
        FLAG_skip_uglify_js = True
    if args.no_gz:
        FLAG_skip_gz_compression = True
    if args.no_image_compress:
        FLAG_skip_image_compress = True
    if args.no_index:
        FLAG_skip_index = True
    if args.force_image:
        FLAG_force_image = True

    calculator_page_sublist = []
    if len(args.limit_files) >= 1:
        FLAG_skip_index = True
        calculator_page_sublist = args.limit_files
        print("Only building", ", ".join(calculator_page_sublist))

    if not FLAG_skip_js_lint:
        lint_javascript()

    if not os.path.exists("output"):
        os.makedirs("output")

    while True:
        # Create the calculators
        d = './resource_lists'
        calculator_directories = []
        for o in os.listdir(d):
            if os.path.isdir(os.path.join(d, o)):
                if calculator_page_sublist == [] or o in calculator_page_sublist:
                    create_calculator_page(o, args.force_html, not args.watch)
                    calculator_directories.append(o)

        if not FLAG_skip_index:
            calculator_directories.sort()
            create_index_page(calculator_directories)

        copy_common_resources()

        if not FLAG_skip_gz_compression:
            pre_compress_output_files()

        if args.watch:
            # If the watch argument is given then poll for changes of the files
            # polling is used instead of something like inotify because change
            # events are not propagated for volumes being run on docker for
            # windows. If ever a nicer solution for handling this appears this
            # code can be changed to support it.
            #
            # NOTE: With this polling method there is a race condition that is
            # possible to hit rather if saving frequently. If a file is
            # updated during its generation, after it has been read but before
            # the first file is written then it will not be detected in the
            # next pass-through.
            time.sleep(.5)
            continue
        else:
            break


main()
