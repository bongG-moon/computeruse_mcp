`recorded-border-template.png` and `recorded-border-screen.png` contain only the
painted “IMAGE ACTION” control from the synthetic recording acceptance fixture.
The screen fixture replaces every other region with a solid background. It has
no application data, user input, window title, account information, or desktop.

The genuine recorder stored a 764×96 row at 512×64 pixels. Driver captured the
same window without the one-pixel left and right border (762 pixels wide).
These pixels exercise border clipping, exact anchor geometry, and refinement of
the sparse feature match before duplicate candidates are merged.
