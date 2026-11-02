// iOS Safari will not start a video that has a <track> in its markup, so the captions are added on play.
const addCaptions = (video, src) => {
  if (video.querySelector("track")) return;
  const track = document.createElement("track");
  track.kind = "captions";
  track.src = src;
  track.srclang = "en";
  track.label = "English";
  video.appendChild(track);
  track.track.mode = "showing";
};

export const DemoVideo = ({ name, label }) => (
  <video
    controls
    preload="none"
    playsInline
    poster={`https://www.getcanonic.app/media/${name}_poster.jpg`}
    aria-label={label}
    style={{ width: "100%", aspectRatio: "16 / 9", borderRadius: "12px", background: "#e8e7e2" }}
    onPlay={(e) => addCaptions(e.currentTarget, `/media/${name}.vtt`)}
  >
    <source src={`https://www.getcanonic.app/media/${name}_web.mp4`} type="video/mp4" />
  </video>
);
