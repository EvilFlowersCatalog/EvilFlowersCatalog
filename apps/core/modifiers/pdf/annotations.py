import fitz


class Shape:

    @staticmethod
    def parse_color(color):
        """
        Convert an SVG color string (e.g., "#RRGGBB") into an RGB tuple (r, g, b).
        Default to black if the color string is not valid.
        """
        if color.startswith("#") and len(color) == 7:
            try:
                return (
                    int(color[1:3], 16) / 255.0,  # Red
                    int(color[3:5], 16) / 255.0,  # Green
                    int(color[5:7], 16) / 255.0,  # Blue
                )
            except ValueError:
                pass  # Fall through to return black
        # Default to black if color parsing fails
        return (0, 0, 0)

    """Base class for all shapes."""

    def draw(self, element, shape_context):
        """Draw the shape on the page. Default: do nothing."""
        pass


class LineShape(Shape):
    def draw(self, element, shape_context):
        x1 = float(element.attrib.get("x1", 0))
        y1 = float(element.attrib.get("y1", 0))
        x2 = float(element.attrib.get("x2", 0))
        y2 = float(element.attrib.get("y2", 0))
        stroke_width = float(element.attrib.get("stroke-width", 1))
        color = (0, 0, 1)  # Default to blue

        shape_context.draw_line((x1, y1), (x2, y2))
        shape_context.finish(width=stroke_width, color=color)


class CircleShape(Shape):
    def draw(self, element, shape_context):
        cx = float(element.attrib.get("cx", 0))
        cy = float(element.attrib.get("cy", 0))
        r = float(element.attrib.get("r", 0))
        fill = element.attrib.get("fill", "black")
        color = self.parse_color(fill)

        shape_context.draw_circle((cx, cy), r)
        shape_context.finish(color=color)


class RectangleShape(Shape):
    def draw(self, element, shape_context):
        x = float(element.attrib.get("x", 0))
        y = float(element.attrib.get("y", 0))
        width = float(element.attrib.get("width", 0))
        height = float(element.attrib.get("height", 0))
        fill = element.attrib.get("fill", "black")
        color = self.parse_color(fill)

        rect = fitz.Rect(x, y, x + width, y + height)
        shape_context.draw_rect(rect)
        shape_context.finish(color=color)


def shape_factory(element):
    """Factory to create appropriate shape object based on SVG element."""
    tag = element.tag.split("}")[-1]  # Handle namespaced tags
    if tag == "line":
        return LineShape()
    elif tag == "circle":
        return CircleShape()
    elif tag == "rect":
        return RectangleShape()
    else:
        return Shape()
