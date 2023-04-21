import resolve from '@rollup/plugin-node-resolve';

export default {
  input: 'examples/web-ifc-viewer/visibility/app.js',
  output: [
    {
      format: 'esm',
      file: 'examples/web-ifc-viewer/visibility/bundle.js'
    },
  ],
  plugins: [
    resolve(),
  ]
};