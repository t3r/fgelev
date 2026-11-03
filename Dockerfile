FROM ubuntu:24.04 AS builder
LABEL maintainer="Torsten Dreyer <torsten@t3r.de>"

ENV DEBIAN_FRONTEND=noninteractive

# Build dependencies for SimGear and fgelev
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    cmake \
    git \
    libbz2-dev \
    libboost-dev \
    libcurl4-openssl-dev \
    liblzma-dev \
    libopenal-dev \
    libexpat1-dev \
    libplib-dev \
    libjpeg-dev \
    libjsoncpp-dev \
    libopenscenegraph-dev \
    libpng-dev \
    zlib1g-dev \
  && rm -rf /var/lib/apt/lists/*

# Build SimGear as static libs so the runtime stage needs no SimGear .so files
RUN git clone --depth 1 -b release/2020.3 --single-branch https://git.code.sf.net/p/flightgear/simgear /simgear \
    && cmake -S /simgear -B /simgear/build \
             -G "Unix Makefiles" \
             -DCMAKE_BUILD_TYPE=Release \
             -DCMAKE_INSTALL_PREFIX=/usr/local \
             -DBUILD_SHARED_LIBS=OFF \
             -DENABLE_RTI=OFF \
             -DENABLE_DNS=ON \
             -DENABLE_TESTS=OFF \
             -DENABLE_SOUND=ON \
             -DUSE_AEONWAVE=OFF \
             -DENABLE_PKGUTIL=OFF \
             -DENABLE_SIMD=OFF \
    && cmake --build /simgear/build -j$(nproc) \
    && cmake --install /simgear/build \
    && rm -rf /simgear

# Build only fgelev from FlightGear — all other targets disabled
RUN git clone --depth 1 -b release/2020.3 --single-branch https://git.code.sf.net/p/flightgear/flightgear /flightgear \
    && sed -i 's/install(TARGETS fgcom fgjs fgelev fgfs/install(TARGETS fgelev/g' /flightgear/CMakeModules/Installation.cmake \
    && sed -i 's/install(TARGETS fgcom fgjs fgelev/install(TARGETS fgelev/g' /flightgear/CMakeModules/Installation.cmake \
    && cmake -S /flightgear -B /flightgear/build \
             -G "Unix Makefiles" \
             -DCMAKE_BUILD_TYPE=Release \
             -DCMAKE_INSTALL_PREFIX=/usr/local \
             -DENABLE_FGELEV=ON \
             -DWITH_FGPANEL=OFF \
             -DENABLE_FGVIEWER=OFF \
             -DENABLE_GPSSMOOTH=OFF \
             -DENABLE_TERRASYNC=OFF \
             -DENABLE_FGJS=OFF \
             -DENABLE_JS_DEMO=OFF \
             -DENABLE_METAR=OFF \
             -DENABLE_STGMERGE=OFF \
             -DENABLE_TESTS=OFF \
             -DENABLE_FGCOM=OFF \
             -DENABLE_FLITE=OFF \
             -DENABLE_QT=OFF \
             -DENABLE_TRAFFIC=OFF \
             -DENABLE_FGQCANVAS=OFF \
             -DENABLE_DEMCONVERT=OFF \
             -DENABLE_HID_INPUT=OFF \
             -DENABLE_PLIB_JOYSTICK=OFF \
    && cmake --build /flightgear/build --target fgelev -j$(nproc) \
    && strip /flightgear/build/utils/fgelev/fgelev \
    && cp /flightgear/build/utils/fgelev/fgelev /usr/local/bin/ \
    && rm -rf /flightgear


FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Runtime dependencies: Ubuntu 24.04 ships all OSG component libs in the
# single libopenscenegraph161 package (libosgText, libosgUtil, etc. are
# all included there — there are no separate per-component packages).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libopenscenegraph161 \
        libcurl4 \
        python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/fgelev /usr/local/bin/fgelev
COPY proxy.py /proxy.py

# OSG's default notify handler sends INFO/DEBUG messages to stdout, which
# pollutes the data stream. WARN level suppresses plugin-loading noise
# at the source without hiding actual errors.
ENV OSG_NOTIFY_LEVEL=WARN

EXPOSE 9000
ENTRYPOINT ["python3", "/proxy.py", "--log-level", "DEBUG"]

