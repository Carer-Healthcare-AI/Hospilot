// "Molten" gooey orb loader for the mission-planning wait -- an SVG mask of
// blurred, independently-rotating polygons pushed back to sharp edges via an
// oscillating contrast filter (the classic gooey-effect trick), so the shapes
// inside the orb continuously melt into and separate from each other. Markup
// only -- see index.css's .coord-loader rules for the actual animation.
export function CoordinatingLoader() {
  return (
    <div className="coord-loader">
      <svg width={100} height={100} viewBox="0 0 100 100">
        <defs>
          <mask id="coord-clipping">
            <polygon points="0,0 100,0 100,100 0,100" fill="black" />
            <polygon points="25,25 75,25 50,75" fill="white" />
            <polygon points="50,25 75,75 25,75" fill="white" />
            <polygon points="35,35 65,35 50,65" fill="white" />
            <polygon points="35,35 65,35 50,65" fill="white" />
            <polygon points="35,35 65,35 50,65" fill="white" />
            <polygon points="35,35 65,35 50,65" fill="white" />
          </mask>
        </defs>
      </svg>
      <div className="coord-loader-box" />
    </div>
  )
}
