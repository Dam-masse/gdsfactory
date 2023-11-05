from __future__ import annotations

from collections.abc import Callable

import numpy as np
from kfactory.routing.optical import OpticalManhattanRoute

import gdsfactory as gf
from gdsfactory.component import Component, ComponentReference
from gdsfactory.components.bend_euler import bend_euler
from gdsfactory.components.straight import straight as straight_function
from gdsfactory.components.taper import taper as taper_function
from gdsfactory.cross_section import strip
from gdsfactory.port import Port, select_ports_optical
from gdsfactory.routing.get_route import place_route
from gdsfactory.routing.utils import direction_ports_from_list_ports
from gdsfactory.typings import ComponentSpec, CrossSectionSpec, Number, Strs


def route_south(
    component: Component,
    component_to_route: Component | ComponentReference,
    optical_routing_type: int = 1,
    excluded_ports: tuple[str, ...] | None = None,
    straight_separation: Number = 4.0,
    io_gratings_lines: list[list[ComponentReference]] | None = None,
    gc_port_name: str = "o1",
    bend: ComponentSpec = bend_euler,
    straight: ComponentSpec = straight_function,
    taper: ComponentSpec | None = taper_function,
    select_ports: Callable = select_ports_optical,
    port_names: Strs | None = None,
    cross_section: CrossSectionSpec = strip,
    start_straight_length: float = 0.5,
    **kwargs,
) -> list[OpticalManhattanRoute]:
    """Places routes to route a component ports to the south.

    Args:
        component: top level component to add the routes.
        component_to_route: component or reference to route.
        optical_routing_type: routing heuristic `1` or `2` \
                `1` uses the component size info to estimate the box size.\
                `2` only looks at the optical port positions to estimate the size.
        excluded_ports: list of port names to NOT route.
        straight_separation: in um.
        io_gratings_lines: list of ports to which the ports produced by this function will be connected. \
                Supplying this information helps avoiding straight collisions.
        gc_port_name: grating coupler port name.
        bend: spec.
        straight: spec.
        taper: spec.
        select_ports: function to select_ports.
        port_names: optional port names. Overrides select_ports.
        cross_section: cross_section spec.
        kwargs: cross_section settings.


    Works well if the component looks roughly like a rectangular box with:
        north ports on the north of the box.
        south ports on the south of the box.
        east ports on the east of the box.
        west ports on the west of the box.

    .. plot::
        :include-source:

        import gdsfactory as gf

        c = gf.components.ring_double()
        c = gf.Component()
        ref = c << gf.components.ring_double()
        r = gf.routing.route_south(ref)
        for e in r.references:
            c.add(e)
        c.plot()

    """
    c = component
    component = component_to_route
    xs = gf.get_cross_section(cross_section)
    excluded_ports = excluded_ports or tuple()
    start_straight_length0 = start_straight_length
    routes = []

    if optical_routing_type not in {
        1,
        2,
    }:
        raise ValueError(
            f"optical_routing_type = {optical_routing_type} not in supported [1, 2]"
        )

    if port_names:
        optical_ports = [component[port_name] for port_name in port_names]
    else:
        optical_ports = select_ports(component.ports)
        optical_ports = [p for p in optical_ports if p.name not in excluded_ports]

    bend90 = bend(cross_section=cross_section, **kwargs) if callable(bend) else bend
    bend90 = gf.get_component(bend90)
    dy = abs(bend90.info["dy"])

    # Handle empty list gracefully
    if not optical_ports:
        return [], []

    conn_params = dict(
        bend=bend,
        straight=straight,
        taper=taper,
        cross_section=cross_section,
        **kwargs,
    )

    # Used to avoid crossing between straights in special cases
    # This could happen when abs(x_port - x_grating) <= 2 * dy
    delta_gr_min = 2 * dy + 1
    sep = straight_separation

    # Get lists of optical ports by orientation
    direction_ports = direction_ports_from_list_ports(optical_ports)

    north_ports = direction_ports["N"]
    north_start = north_ports[: len(north_ports) // 2]
    north_finish = north_ports[len(north_ports) // 2 :]

    west_ports = direction_ports["W"]
    west_ports.reverse()
    east_ports = direction_ports["E"]
    south_ports = direction_ports["S"]
    north_finish.reverse()  # Sort right to left
    north_start.reverse()  # Sort right to left
    ordered_ports = north_start + west_ports + south_ports + east_ports + north_finish

    def get_index_port_closest_to_x(x, list_ports):
        return np.array(
            [abs(x - p.ports[gc_port_name].d.x) for p in list_ports]
        ).argmin()

    def gen_port_from_port(x, y, p, cross_section):
        return Port(
            name=p.name,
            center=(x, y),
            orientation=90.0,
            width=p.d.width,
            cross_section=cross_section,
        )

    west_ports.reverse()
    y0 = min(p.d.y for p in ordered_ports) - dy - 0.5
    ports_to_route = []

    optical_xs_tmp = [p.d.x for p in ordered_ports]
    x_optical_min = min(optical_xs_tmp)
    x_optical_max = max(optical_xs_tmp)

    # Set starting ``x`` on the west side
    # ``x`` is the x-coord of the waypoint where the current component port is connected.
    # x starts as close as possible to the component.
    # For each new port, the distance is increased by the separation.
    # The starting x depends on the heuristic chosen : ``1`` or ``2``
    if optical_routing_type == 1:
        # use component size to know how far to route
        x = component.d.xmin - dy - 1
    elif optical_routing_type == 2:
        # use optical port to know how far to route
        x = x_optical_min - dy - 1
    else:
        raise ValueError(
            f"Invalid optical routing type {optical_routing_type!r} not in [1, 2]"
        )

    # First route the ports facing west
    # In case we have to connect these ports to a line of gratings,
    # Ensure that the port is aligned with the grating port or
    # has enough space for manhattan routing (at least two bend radius)
    for p in west_ports:
        if io_gratings_lines:
            i_grating = get_index_port_closest_to_x(x, io_gratings_lines[-1])
            x_gr = io_gratings_lines[-1][i_grating].ports[gc_port_name].d.x
            if abs(x - x_gr) < delta_gr_min:
                if x > x_gr:
                    x = x_gr
                elif x < x_gr:
                    x = x_gr - delta_gr_min

        tmp_port = gen_port_from_port(x, y0, p, cross_section=xs)
        ports_to_route.append(tmp_port)
        route = place_route(c, p, tmp_port, **conn_params)
        x -= sep

    # route first halft of north ports above the top west one
    north_start.reverse()  # We need them from left to right

    start_straight_length = start_straight_length0
    if len(north_start) > 0:
        y_max = max(p.d.y for p in west_ports + north_start)
        for p in north_start:
            tmp_port = gen_port_from_port(x, y0, p, cross_section=xs)
            route = place_route(
                component=c,
                port1=p,
                port2=tmp_port,
                start_straight_length=start_straight_length + y_max - p.d.y,
                **conn_params,
            )

            ports_to_route.append(tmp_port)
            x -= sep
            start_straight_length += sep
            routes.append(route)

    # Set starting ``x`` on the east side
    if optical_routing_type == 1:
        #  use component size to know how far to route
        x = component.d.xmax + dy + 1
    elif optical_routing_type == 2:
        # use optical port to know how far to route
        x = x_optical_max + dy + 1
    else:
        raise ValueError(
            f"Invalid optical routing type. Got {optical_routing_type}, only (1, 2 supported) "
        )

    # Route the east ports
    # In case we have to connect these ports to a line of gratings,
    # Ensure that the port is aligned with the grating port or
    # has enough space for manhattan routing (at least two bend radius)
    start_straight_length = start_straight_length0
    for p in east_ports:
        if io_gratings_lines:
            i_grating = get_index_port_closest_to_x(x, io_gratings_lines[-1])
            x_gr = io_gratings_lines[-1][i_grating].ports[gc_port_name].d.x
            if abs(x - x_gr) < delta_gr_min:
                if x < x_gr:
                    x = x_gr
                elif x > x_gr:
                    x = x_gr + delta_gr_min

        tmp_port = gen_port_from_port(x, y0, p, cross_section=xs)
        route = place_route(
            c,
            p,
            tmp_port,
            start_straight_length=start_straight_length,
            **conn_params,
        )
        routes.append(route)
        ports_to_route.append(tmp_port)
        x += sep

    # Route the remaining north ports
    start_straight_length = start_straight_length0
    if len(north_finish) > 0:
        y_max = max(p.d.y for p in east_ports + north_finish)
        for p in north_finish:
            tmp_port = gen_port_from_port(x, y0, p, cross_section=xs)
            ports_to_route.append(tmp_port)
            route = place_route(
                c,
                p,
                tmp_port,
                start_straight_length=start_straight_length + y_max - p.d.y,
                **conn_params,
            )
            x += sep
            start_straight_length += sep
            routes.append(route)

    flipped_ports = [p.copy() for p in ports_to_route]
    for p in flipped_ports:
        p.trans *= gf.kdb.Trans.R180
    c.add_ports(flipped_ports)
    c.add_ports(south_ports)
    c.auto_rename_ports()
    return routes


if __name__ == "__main__":
    layer = (2, 0)
    c = gf.Component()
    component = gf.components.ring_double(layer=layer)
    component = gf.components.nxn(north=2, south=2, west=2)
    ref = c << component
    r = route_south(c, ref, optical_routing_type=1, start_straight_length=0)
    # print(r.lengths)
    c.show()
