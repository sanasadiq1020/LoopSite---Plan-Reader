"use client";

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

/**
 * The generated wall model, in the browser.
 *
 * Each wall arrives as its own named node in the GLB, named with the
 * ``element_id`` from the canonical model. That is what lets a click on a wall
 * say which wall it is — and therefore which two lines on which sheet it was
 * measured from. A viewer that could only show a grey shape would prove
 * nothing; being able to point at one wall and trace it back to the drawing is
 * the whole of the Day 5 checkpoint.
 *
 * The scene is deliberately plain. Week 1 asks whether the structured data can
 * be built from, not for a rendering.
 */
export function ModelViewer({
  url,
  onWallSelected,
  selectedId,
}: {
  url: string;
  onWallSelected: (elementId: string | null) => void;
  selectedId: string | null;
}) {
  const mountRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "failed">("loading");
  const [error, setError] = useState<string | null>(null);
  // Kept in refs so the highlight effect can reach them without rebuilding
  // the whole scene every time a wall is clicked.
  const wallsRef = useRef<THREE.Mesh[]>([]);
  const selectRef = useRef(onWallSelected);
  // Updated in an effect rather than during render: the scene is built once
  // and has to keep reaching the current handler without being rebuilt.
  useEffect(() => {
    selectRef.current = onWallSelected;
  }, [onWallSelected]);

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xf1f5f9);

    const camera = new THREE.PerspectiveCamera(
      45,
      mount.clientWidth / Math.max(mount.clientHeight, 1),
      10,
      1_000_000
    );

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(mount.clientWidth, mount.clientHeight);
    mount.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;

    // Enough light to read the shape of a wall without pretending to render.
    scene.add(new THREE.HemisphereLight(0xffffff, 0x94a3b8, 2.2));
    const sun = new THREE.DirectionalLight(0xffffff, 1.4);
    sun.position.set(1, 2, 1);
    scene.add(sun);

    let frame = 0;
    let disposed = false;

    const loader = new GLTFLoader();
    // Every output file is served through an authenticated route, so the
    // loader has to send the session cookie. Without this the request goes
    // out without it, the run is not recognised as this session's, and the
    // model comes back as "not found".
    loader.setWithCredentials(true);
    loader.load(
      url,
      (gltf) => {
        if (disposed) return;
        scene.add(gltf.scene);

        const walls: THREE.Mesh[] = [];
        gltf.scene.traverse((child) => {
          if ((child as THREE.Mesh).isMesh) {
            const mesh = child as THREE.Mesh;
            // Its own material, so highlighting one wall does not light up
            // every wall that happens to share the material.
            mesh.material = new THREE.MeshStandardMaterial({
              color: 0xcbd5e1,
              roughness: 0.85,
              metalness: 0,
            });
            walls.push(mesh);
          }
        });
        wallsRef.current = walls;

        // Frame the building, whatever size it is — a house and a shed both
        // have to arrive filling the view.
        const box = new THREE.Box3().setFromObject(gltf.scene);
        const size = box.getSize(new THREE.Vector3());
        const centre = box.getCenter(new THREE.Vector3());
        const span = Math.max(size.x, size.y, size.z) || 1000;

        // A ground plane and a grid, so the model has somewhere to stand.
        const grid = new THREE.GridHelper(span * 2.5, 24, 0x94a3b8, 0xd8e0ea);
        grid.position.set(centre.x, box.min.y, centre.z);
        scene.add(grid);

        camera.near = span / 500;
        camera.far = span * 50;
        camera.position.set(centre.x + span * 0.9, box.max.y + span * 0.8, centre.z + span * 1.1);
        camera.updateProjectionMatrix();
        controls.target.copy(centre);
        controls.update();

        setStatus("ready");
      },
      undefined,
      (loadError) => {
        if (disposed) return;
        setError(
          loadError instanceof Error
            ? loadError.message
            : "The 3D file could not be loaded."
        );
        setStatus("failed");
      }
    );

    // --- clicking a wall -------------------------------------------------
    const pointer = new THREE.Vector2();
    const raycaster = new THREE.Raycaster();
    let pressedAt: { x: number; y: number } | null = null;

    const onPointerDown = (event: PointerEvent) => {
      pressedAt = { x: event.clientX, y: event.clientY };
    };
    const onPointerUp = (event: PointerEvent) => {
      // A drag is how the model is turned; only a real click selects.
      if (
        !pressedAt ||
        Math.abs(event.clientX - pressedAt.x) > 4 ||
        Math.abs(event.clientY - pressedAt.y) > 4
      ) {
        pressedAt = null;
        return;
      }
      pressedAt = null;
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const hits = raycaster.intersectObjects(wallsRef.current, false);
      selectRef.current(hits.length ? hits[0].object.name : null);
    };
    renderer.domElement.addEventListener("pointerdown", onPointerDown);
    renderer.domElement.addEventListener("pointerup", onPointerUp);

    const resize = () => {
      if (!mount.clientWidth || !mount.clientHeight) return;
      camera.aspect = mount.clientWidth / mount.clientHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(mount.clientWidth, mount.clientHeight);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(mount);

    const animate = () => {
      frame = requestAnimationFrame(animate);
      controls.update();
      renderer.render(scene, camera);
    };
    animate();

    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      observer.disconnect();
      renderer.domElement.removeEventListener("pointerdown", onPointerDown);
      renderer.domElement.removeEventListener("pointerup", onPointerUp);
      controls.dispose();
      // WebGL contexts are a limited resource; a viewer that leaks them stops
      // working after a handful of sheets have been opened.
      scene.traverse((child) => {
        const mesh = child as THREE.Mesh;
        if (mesh.isMesh) {
          mesh.geometry?.dispose();
          const material = mesh.material;
          if (Array.isArray(material)) material.forEach((m) => m.dispose());
          else material?.dispose();
        }
      });
      renderer.dispose();
      if (renderer.domElement.parentNode === mount) {
        mount.removeChild(renderer.domElement);
      }
      wallsRef.current = [];
    };
  }, [url]);

  // The selected wall is coloured here rather than in the loader, so choosing
  // a wall from the table on the left highlights it in the view too.
  useEffect(() => {
    for (const mesh of wallsRef.current) {
      const material = mesh.material as THREE.MeshStandardMaterial;
      if (!material?.color) continue;
      material.color.set(mesh.name === selectedId ? 0x2563eb : 0xcbd5e1);
    }
  }, [selectedId, status]);

  return (
    <div className="relative h-[26rem] w-full overflow-hidden rounded-xl border border-slate-200 bg-slate-100 lg:h-[34rem]">
      <div ref={mountRef} className="h-full w-full" />

      {status === "loading" && (
        <div className="absolute inset-0 flex items-center justify-center gap-3 bg-slate-100/80 text-sm text-slate-600">
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-blue-600 border-t-transparent" />
          Loading the model…
        </div>
      )}

      {status === "failed" && (
        <div className="absolute inset-0 flex items-center justify-center p-6">
          <p className="max-w-sm rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-center text-sm text-rose-800">
            The 3D model could not be displayed.
            {error && <span className="mt-1 block text-xs text-rose-600">{error}</span>}
          </p>
        </div>
      )}

      {status === "ready" && (
        <p className="pointer-events-none absolute bottom-2 left-1/2 -translate-x-1/2 rounded-full bg-white/85 px-3 py-1 text-xs text-slate-500 shadow-sm">
          Drag to turn · scroll to zoom · click a wall to see where it came from
        </p>
      )}
    </div>
  );
}
