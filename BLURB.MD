Subject: [OC] rmbloat (beta) mass video converter for aged collections

r/commandline
r/DataHoarder
r/selfhosted
r/PleX
r/jellyfin
r/emby
r/HomeServer


I had a ton video files on BDs and old drives, and the thought of converting them was always overwhelming. Some didn't play well on my TV box because the codec was ancient. Every time I looked at the library, I just saw a massive, months-long chore.

I looked at tools like Tdarr and Unmanic and felt they were overkill for my needs. I didn't want to manage a web server, databases, or complex nodes just to shrink my TV shows. I wanted a fast, mass or surgical tool that lives in the terminal, tells me exactly how "bloated" a file is, and lets me reclaim space with one keypress.

So I wrote `rmbloat`. I can fire this up for 10,000 files, park it in tmux, and come back a week later to see how many GBs were shaved off. Or I just use it for a single season I just copied from BD.


**Why use rmbloat?**

* Speed: If you have an 11th Gen or newer Intel CPU (QuickSync/VA-API), the throughput is incredible. My "months-long" conversion ended up taking weeks, often hitting 25x–30x speeds on 1080p/480p content.
* The "Bloat" Metric: It calculates a score to show you which files are the biggest waste of space, so it can prioritize the high-yield conversions first.
* Resilience: It has a "retry ladder." If hardware acceleration fails on a corrupt or weird file, it automatically falls back to software to ensure the queue doesn't stall.
* Low Impact: It defaults to nice and ionice priorities, meaning your server stays responsive for streaming while the background thread chugs away.
* Zero Infrastructure: No background services or web GUIs. It’s a Python app you run when you need it and close when you’re done.

**And why not? Beta Status & Limitations**

I'm calling this Beta because while it’s been rock-solid for my library, I’ve mostly tuned it for Intel VA-API/QuickSync. I don't have an NVIDIA rig to test, so I'm not making any big claims there — though it has a CPU fallback.

And, although there are lots of options, it is very opinionated.


Demo GIF: [rmbloat-demo](https://raw.githubusercontent.com/joedefen/rmbloat/main/image/rmbloat-2026-01-09-13-30.gif)


For more info, I'll refer you to the docs, [rmbloat](https://github.com/joedefen/rmbloat).
