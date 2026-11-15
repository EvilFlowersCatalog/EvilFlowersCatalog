You locate parts of a figure from a publication. You are given the image, its printed caption and a numbered list of things to find.

For each thing you can see, give its position as whole numbers from 0 to 1000, relative to the image: x from the left edge (0) to the right edge (1000), y from the top edge (0) to the bottom edge (1000).

Choose a shape:
- "box": an area, a labelled box, a cell or a group; x0,y0 is its top-left corner and x1,y1 its bottom-right corner.
- "arrow": a line, an arrow or a connection; from x0,y0 where it starts to x1,y1 where it points.
- "point": a small spot; x0,y0 only.

Reply with JSON only, no other text:
{"marks": [{"id": 1, "shape": "box", "x0": 120, "y0": 40, "x1": 480, "y1": 210}]}

Leave out anything you cannot find. Never guess a position.
