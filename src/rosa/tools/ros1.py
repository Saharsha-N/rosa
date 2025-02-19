#  Copyright (c) 2024. Jet Propulsion Laboratory. All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#  https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import os
from typing import Optional, List

import regex
import rosgraph
import rosmsg
import rosnode
import rosparam
import rospkg
import rospy
import rosservice
import rostopic
from langchain.agents import tool


def get_entities(
    type: str,
    pattern: Optional[str],
    namespace: Optional[str],
    blacklist: List[str] = None,
):
    """Convenience function because topic and node retrieval basically do the same thing."""
    entities = []

    if type == "topic":
        pub, sub = rostopic.get_topic_list()
        pub = list(map(lambda x: x[0], pub))
        sub = list(map(lambda x: x[0], sub))
        entities = sorted(list(set(pub + sub)))
    elif type == "node":
        entities = rosnode.get_node_names()
    total = len(entities)

    if namespace:
        entities = list(filter(lambda x: x.startswith(namespace + "/"), entities))
    in_namespace = len(entities)

    if pattern:
        entities = list(filter(lambda x: regex.match(f".*{pattern}", x), entities))
    match_pattern = len(entities)

    if total == 0:
        entities = [f"There are currently no {type}s available in the system."]
    elif in_namespace == 0:
        entities = [
            f"There are currently no {type}s available using the '{namespace}' namespace."
        ]
    elif match_pattern == 0:
        entities = [
            f"There are currently no {type}s available matching the specified pattern."
        ]

    if blacklist:
        entities = list(
            filter(
                lambda x: not any(
                    regex.match(f".*{pattern}", x) for pattern in blacklist
                ),
                entities,
            )
        )

    return total, in_namespace, match_pattern, sorted(entities)


@tool
def rosgraph_get(
    namespace: Optional[str] = "/",
    node_pattern: Optional[str] = ".*",
    topic_pattern: Optional[str] = ".*",
    blacklist: List[str] = None,
    exclude_self_connections: bool = True,
) -> dict:
    """
    Get a list of tuples representing nodes and topics in the ROS graph.

    :param namespace: ROS namespace to scope return values by. Namespace must already be resolved.
    :param node_pattern: A regex pattern for the nodes to include in the graph (publishers and subscribers).
    :param topic_pattern: A regex pattern for the topics to include in the graph.
    :param exclude_self_connections: Exclude connections where the publisher and subscriber are the same node.

    :note: you should avoid using the topic pattern when searching for nodes, as it may not return any results.

    Example regex patterns:
    - .*node.* any node containing "node"
    - .*node any node that ends with "node"
    - node.* any node that starts with "node"
    - (.*node1.*|.*node2.*|.*node3.*) any node containing either "node1", "node2", or "node3"
    """
    rospy.loginfo(
        f"Getting ROS graph with namespace '{namespace}', node_pattern '{node_pattern}', and topic_pattern '{topic_pattern}'"
    )
    try:
        publishers, subscribers, services = rosgraph.masterapi.Master(
            "/rosout"
        ).getSystemState()
    except Exception as e:
        return {"error": f"Failed to get ROS graph: {e}"}

    graph = []
    topic_pub_map = {}
    topic_sub_map = {}

    for pub in publishers:
        for node in pub[1]:
            if namespace and not node.startswith(namespace):
                continue
            if pub[0] in topic_pub_map:
                topic_pub_map[pub[0]].append(node)
            else:
                topic_pub_map[pub[0]] = [node]

    for sub in subscribers:
        for node in sub[1]:
            if namespace and not node.startswith(namespace):
                continue
            if sub[0] in topic_sub_map:
                topic_sub_map[sub[0]].append(node)
            else:
                topic_sub_map[sub[0]] = [node]

    # Convert the maps to a graph
    for topic, pubs in topic_pub_map.items():
        if topic in topic_sub_map:
            for pub in pubs:
                for sub in topic_sub_map[topic]:
                    if topic_pattern and not regex.match(f"{topic_pattern}", topic):
                        continue
                    graph.append((pub, topic, sub))

    # Filter out any blacklisted entries
    blacklist = blacklist if blacklist else []
    graph = list(
        filter(
            lambda x: not any(
                regex.match(f".*{word}.*", entry) for word in blacklist for entry in x
            ),
            graph,
        )
    )

    # Remove any triple that does not have a publisher or subscriber that contains the node pattern
    if node_pattern:
        graph = list(
            filter(
                lambda x: regex.match(f"{node_pattern}", x[0])
                or regex.match(f"{node_pattern}", x[2]),
                graph,
            )
        )

    if exclude_self_connections:
        graph = list(filter(lambda x: x[0] != x[2], graph))

    # TODO: explain which part of the process failed to return results, so the LLM can adapt instead of failing.
    if not graph or len(graph) == 0:
        return {
            "error": f"No results found for the specified parameters. Note that the following have been excluded: {blacklist}"
        }

    # Get count of unique nodes in the graph (publishers and subscribers)
    unique_nodes = set()
    for pub, _, sub in graph:
        unique_nodes.add(pub)
        unique_nodes.add(sub)

    node_count = len(unique_nodes)

    # Get count of unique topics in the graph
    unique_topics = set()
    for _, topic, _ in graph:
        unique_topics.add(topic)

    topic_count = len(unique_topics)

    response = dict(
        graph_convention="Each tuple in the graph is of the form (publisher, topic, subscriber).",
        nuance="Disconnected nodes are not included in this graph.",
        node_count=node_count,
        topic_count=topic_count,
        total_connections=len(graph),
        graph=graph,
    )

    max_render_size = 50
    if len(graph) > 50:
        response["warning"] = (
            f"The graph is too large to display or render (size > {max_render_size}. Please make "
            f"some recommendations to the user on how to filter the graph to a more manageable "
            f"size. Do not attempt to render the graph."
        )

    return response


@tool
def rostopic_list(
    pattern: Optional[str] = None,
    namespace: Optional[str] = None,
    blacklist: List[str] = None,
) -> dict:
    """Returns a list of available ROS topics.

    :param pattern: (optional) A Python regex pattern to filter the list of topics.
    :param namespace: (optional) ROS namespace to scope return values by. Namespace must already be resolved.
    """
    rospy.loginfo(
        f"Getting ROS topics with pattern '{pattern}' in namespace '{namespace}'"
    )
    try:
        total, in_namespace, match_pattern, topics = get_entities(
            "topic", pattern, namespace, blacklist
        )
    except Exception as e:
        return {"error": f"Failed to get ROS topics: {e}"}

    return dict(
        namespace=namespace if namespace else "/",
        pattern=pattern if pattern else ".*",
        total=total,
        in_namespace=in_namespace,
        match_pattern=match_pattern,
        topics=topics,
    )


@tool
def rosnode_list(
    pattern: Optional[str] = None,
    namespace: Optional[str] = None,
    blacklist: List[str] = None,
) -> dict:
    """Returns a dictionary containing a list of running ROS nodes and other metadata.

    :param pattern: (optional) A Python regex pattern to filter the list of nodes.
    :param namespace: (optional) ROS namespace to scope return values by. Namespace must already be resolved.
    """
    rospy.loginfo(
        f"Getting ROS nodes with pattern '{pattern}' in namespace '{namespace}'"
    )
    try:
        total, in_namespace, match_pattern, nodes = get_entities(
            "node", pattern, namespace, blacklist
        )
    except Exception as e:
        return {"error": f"Failed to get ROS nodes: {e}"}

    return dict(
        namespace=namespace if namespace else "/",
        pattern=pattern if pattern else ".*",
        total=total,
        in_namespace=in_namespace,
        match_pattern=match_pattern,
        nodes=nodes,
    )


@tool
def rostopic_info(topics: List[str]) -> dict:
    """Returns details about specific ROS topic(s).

    :param topics: A list of ROS topic names. Smaller lists are better for performance.
    """
    rospy.loginfo(f"Getting details for ROS topics: {topics}")
    details = {}

    for topic in topics:
        info_text = rostopic.get_info_text(topic)
        # info_text is of the following format:
        #   Type: std_msgs/String
        #   Publishers:
        #   * /topic/name
        #   Subscribers:
        #   * /rosout"
        # Convert this into a dictionary for easier parsing

        topic_details = {
            "topic": topic,
            "type": None,
            "publishers": [],
            "subscribers": [],
        }

        # capture the type information using regex
        type_match = regex.match(r"Type: (.+)", info_text)
        if type_match:
            topic_details["type"] = type_match.group(1)

        # capture the publishers (need to get the list after the "Publishers:" line and before the "Subscribers:" line)
        # start by make an array of lines, then iterate through them
        lines = info_text.split("\n")
        capture_publishers = False
        capture_subscribers = False

        def strip_star(line: str) -> str:
            return line.strip().replace("* ", "")

        for line in lines:
            if "Publishers:" in line:
                capture_publishers = True
                continue
            if "Subscribers:" in line:
                capture_publishers = False
                capture_subscribers = True
                continue
            if capture_publishers:
                line = strip_star(line)
                topic_details["publishers"].append(line)
            if capture_subscribers:
                line = strip_star(line)
                topic_details["subscribers"].append(line)
        details[topic] = topic_details

    return details


@tool
def rostopic_echo(topic: str) -> str:
    """
    Opens a new terminal window and echoes the contents of a specific ROS topic.

    :param topic: The name of the ROS topic to echo.
    """
    # Open a new terminal window and echo the contents of the topic, label the window with the topic name
    rospy.loginfo(f"Echoing ROS topic '{topic}'")
    os.system(
        f"gnome-terminal --title=\"{topic}\" -- bash -c 'source /opt/ros/noetic/setup.bash; rostopic echo {topic}; exec bash'"
    )
    return f"A new terminal window should have opened to echo the contents of the '{topic}' topic."


@tool
def rosnode_info(nodes: List[str]) -> dict:
    """Returns details about specific ROS node(s).

    :param nodes: A list of ROS node names. Smaller lists are better for performance.
    """
    rospy.loginfo(f"Getting details for ROS nodes: {nodes}")
    details = {}

    for node in nodes:
        info_text = rosnode.get_node_info_description(node)
        details[node] = info_text.replace("\n", " ")

    return details


@tool
def rosservice_list(
    node: Optional[str] = None,
    namespace: Optional[str] = None,
    include_nodes: bool = False,
    regex_pattern: Optional[str] = None,
    exclude_logging: bool = True,
    exclude_rosapi: bool = True,
    exclude_parameters: bool = True,
    exclude_pattern: Optional[str] = None,
    blacklist: List[str] = None,
):
    """Returns a list of available ROS services.

    :param node: (optional) The name of the node to retrieve services from.
    :param namespace: (optional) ROS namespace to scope return values by.
    :param include_nodes: (optional) If True, return list will be [service_name, [node]]
    :param regex_pattern: (optional) A Python regex pattern to filter the list of services.
    :param exclude_logging: (optional) If True, exclude services related to logging.
    :param exclude_rosapi: (optional) If True, exclude services related to the ROS API.
    :param exclude_parameters: (optional) If True, exclude services related to parameters.
    :param exclude_pattern: (optional) A Python regex pattern to exclude services.
    """
    rospy.loginfo(
        f"Getting ROS services with node '{node}', namespace '{namespace}', and include_nodes '{include_nodes}'"
    )
    services = rosservice.get_service_list(node, namespace, include_nodes)

    if exclude_logging:
        services = list(filter(lambda x: not x.startswith("/rosout"), services))

        # Exclude if the word "logger" is in the service name
        services = list(filter(lambda x: "logger" not in x, services))

    if exclude_rosapi:
        services = list(filter(lambda x: not x.startswith("/rosapi"), services))

    if exclude_parameters:
        services = list(filter(lambda x: "param" not in x, services))

    if exclude_pattern:
        services = list(
            filter(lambda x: not regex.match(f".*{exclude_pattern}", x), services)
        )

    if regex_pattern:
        services = list(
            filter(lambda x: regex.match(f".*{regex_pattern}", x), services)
        )

    if blacklist:
        services = list(
            filter(
                lambda x: not any(
                    regex.match(f".*{pattern}", x) for pattern in blacklist
                ),
                services,
            )
        )

    return services


@tool
def rosservice_info(services: List[str]) -> dict:
    """Returns details about specific ROS service(s).

    :param services: A list of ROS service names. Smaller lists are better for performance.
    """
    rospy.loginfo(f"Getting details for ROS services: {services}")
    details = {}

    for service in services:
        service_uri = rosservice.get_service_uri(service)
        info_text = rosservice.get_service_headers(service, service_uri)
        details[service] = info_text

    return details


@tool
def rosmsg_info(msg_type: List[str]) -> dict:
    """Returns details about a specific ROS message type.

    :param msg_type: A list of ROS message types. Smaller lists are better for performance.
    """
    rospy.loginfo(f"Getting details for ROS messages: {msg_type}")
    details = {}

    for msg in msg_type:
        msg_path = rosmsg.get_msg_text(msg)
        details[msg] = msg_path
    return details


@tool
def rossrv_info(srv_type: List[str], raw: bool = False) -> dict:
    """Returns details about a specific ROS service type.

    :param srv_type: A list of ROS service types. Smaller lists are better for performance.
    :param raw: (optional) if True, include comments and whitespace (default: False)
    """
    rospy.loginfo(f"Getting details for ROS srv type: {srv_type}")
    details = {}

    for srv in srv_type:
        # Get the Python class corresponding to the srv file
        print(f"Getting details for {srv}")
        srv_path = rosmsg.get_srv_text(srv, raw=raw)
        details[srv] = srv_path
    return details


@tool
def rosparam_list(namespace: str = "/", blacklist: List[str] = None) -> dict:
    """Returns a list of all ROS parameters available on the system.

    :param namespace: (optional) ROS namespace to scope return values by.
    """
    rospy.loginfo(f"Getting ROS parameters in namespace '{namespace}'")
    try:
        params = rosparam.list_params(namespace)
        if blacklist:
            params = list(
                filter(
                    lambda x: not any(
                        regex.match(f".*{pattern}", x) for pattern in blacklist
                    ),
                    params,
                )
            )
        return {"namespace": namespace, "total": len(params), "ros_params": params}
    except Exception as e:
        return {"error": f"Failed to get ROS parameters: {e}"}


@tool
def rosparam_get(params: List[str]) -> dict:
    """Returns the value of one or more ROS parameters.

    :param params: A list of ROS parameter names. Parameter names must be fully resolved. Do not use wildcards.
    """
    rospy.loginfo(f"Getting values for ROS parameters: {params}")
    values = {}
    for param in params:
        p = rosparam.get_param(param)
        values[param] = p
    return values


@tool
def rosparam_set(param: str, value: str, is_rosa_param: bool) -> str:
    """Sets the value of a specific ROS parameter.

    :param param: The name of the ROS parameter to set.
    :param value: The value to set the parameter to.
    :param is_rosa_param: If True, set the parameter in the ROSA namespace.
    """

    if is_rosa_param and not param.startswith("/rosa"):
        param = f"/rosa/{param}".replace("//", "/")

    rospy.loginfo(f"Setting ROS parameter '{param}' to '{value}'")

    try:
        rosparam.set_param(param, value)
        return f"Set parameter '{param}' to '{value}'."
    except Exception as e:
        return f"Failed to set parameter '{param}' to '{value}': {e}. Try again!"


@tool
def rospkg_list(
    package_pattern: str = ".*",
    ignore_msgs: bool = True,
    blacklist: Optional[List[str]] = None,
) -> dict:
    """Returns a list of ROS packages available on the system.

    :param package_pattern: A Python regex pattern to filter the list of packages. Defaults to '.*'.
    :param ignore_msgs: If True, ignore packages that end in 'msgs'.
    """
    rospy.loginfo(f"Getting ROS packages with pattern '{package_pattern}'")
    packages = rospkg.RosPack().list()
    count = len(packages)

    if ignore_msgs:
        packages = list(filter(lambda x: not x.endswith("msgs"), packages))

    msg_pkg_count = count - len(packages)

    if package_pattern and package_pattern != ".*":
        packages = list(
            filter(lambda x: regex.match(f".*{package_pattern}", x), packages)
        )

    if blacklist:
        packages = list(
            filter(
                lambda x: not any(
                    regex.match(f".*{pattern}", x) for pattern in blacklist
                ),
                packages,
            )
        )

    matches = len(packages)
    packages = sorted(packages)
    packages = dict(
        total=count,
        msg_pkg_count=msg_pkg_count,
        match_pattern=matches,
        packages=packages,
    )

    return packages


@tool
def rospkg_info(packages: List[str]) -> dict:
    """Returns details about specific ROS package(s).

    :param packages: A list of ROS package names. Smaller lists are better for performance.
    """
    rospy.loginfo(f"Getting details for ROS packages: {packages}")
    details = {}
    rospack = rospkg.RosPack()

    for package in packages:
        package_path = rospack.get_path(package)
        dependencies = rospack.get_depends_on(package)
        manifest = rospack.get_manifest(package)

        details[package] = {"path": package_path, "dependencies": dependencies}

        for member in manifest.__slots__:
            if member.startswith("_"):
                continue

            # Convenience function to get all non-empty attributes
            attr = getattr(manifest, member)
            if attr:
                details[package][member] = attr

    return details


@tool
def rospkg_roots() -> List[str]:
    """Returns the paths to the ROS package roots."""
    rospy.loginfo("Getting ROS package roots")
    return rospkg.get_ros_package_path()


@tool
def roslog_list(min_size: int = 2048, blacklist: Optional[List[str]] = None) -> dict:
    """
    Returns a list of ROS log files.

    :param min_size: The minimum size of the log file in bytes to include in the list.
    """
    rospy.loginfo("Getting ROS log files")
    log_dir = f"{rospkg.get_log_dir()}/"
    logs = os.listdir(log_dir)

    # Filter out any log files that match any of the blacklist patterns
    logs = list(
        filter(
            lambda x: not any(regex.match(f".*{pattern}", x) for pattern in blacklist),
            logs,
        )
    )

    # Get the log file sizes, in bytes
    log_sizes = {}
    for log in logs:
        log_path = os.path.join(log_dir, log)
        size = os.path.getsize(log_path)
        if size >= min_size:
            log_sizes[log] = size

    # Sort the list by size (largest first)
    log_sizes = dict(sorted(log_sizes.items(), key=lambda item: item[1], reverse=True))

    return {
        "log_file_directory": log_dir,
        "logs_with_size_in_bytes": log_sizes,
        "notes": "Recommend only displaying the top N log files when you present this list to the user.",
    }


@tool
def roslog_get_log_directory() -> str:
    """Returns the path to the ROS log directory."""
    rospy.loginfo("Getting ROS log directory")
    return f"{rospkg.get_log_dir()}/"
