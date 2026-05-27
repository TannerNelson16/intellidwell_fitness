self.addEventListener("push", (event) => {
  let data = {};
  if (event.data) {
    try {
      data = event.data.json();
    } catch (err) {
      data = { title: "Intellidwell", body: event.data.text() };
    }
  }
  const title = data.title || "Intellidwell Fitness";
  const options = {
    body: data.body || "Keep showing up.",
    icon: "/static/img/icon-192.png",
    badge: "/static/img/icon-192.png",
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: "window" }).then((clientList) => {
      for (const client of clientList) {
        if (client.url.includes("/")) {
          return client.focus();
        }
      }
      return clients.openWindow("/");
    })
  );
});
