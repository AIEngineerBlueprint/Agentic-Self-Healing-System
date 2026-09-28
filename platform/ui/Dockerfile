FROM node:22-alpine AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
ARG VITE_CTRL_BASE=http://localhost:8090
ARG VITE_PRODUCT_BASE=http://localhost:3000
ENV VITE_CTRL_BASE=$VITE_CTRL_BASE VITE_PRODUCT_BASE=$VITE_PRODUCT_BASE
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 3001
