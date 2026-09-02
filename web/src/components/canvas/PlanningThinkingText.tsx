import { useEffect, useState } from 'react'

// Claude-style "Thinking…" shimmer -- a single playful word cycling with a
// sweeping gradient, instead of a static label or a full rotating sentence.
const WORDS = [
	'Thinking',
	'Orchestrating',
	'Coordinating',
	'Strategizing',
	'Deliberating',
	'Synthesizing',
	'Noodling',
	'Percolating',
	'Calibrating',
	'Triangulating',
]

export function PlanningThinkingText() {
	const [index, setIndex] = useState(0)

	useEffect(() => {
		const cycle = setInterval(() => {
			setIndex((i) => (i + 1) % WORDS.length)
		}, 1800)
		return () => clearInterval(cycle)
	}, [])

	return (
		<div className="planning-shimmer-text text-base font-semibold">
			{WORDS[index]}…
		</div>
	)
}
